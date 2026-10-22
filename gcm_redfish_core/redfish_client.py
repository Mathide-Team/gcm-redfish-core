"""Client Redfish générique, indépendant du constructeur.

Ce module s'appuie uniquement sur les ressources standard du schéma DMTF
Redfish (``ServiceRoot``, ``Systems``, ``UpdateService/FirmwareInventory``).
Aucune extension OEM n'est utilisée ici : les spécificités HPE iLO / Dell
iDRAC / Lenovo XCC (Virtual Media avancé, consoles propriétaires, capteurs
détaillés) seront isolées dans des classes d'adaptation séparées une fois ce
socle validé — c'est volontairement la partie la plus "portable" du projet.

Point de conception important : ce client ne fait plus référence au moteur
de dépendances (abandonné). Il produit un ``ServerProfile`` — un inventaire —
et rien de plus.
"""

from __future__ import annotations

from typing import Any

import requests
import urllib3
from loguru import logger

from .catalog_import import classify_dell_component, classify_hpe_component, classify_lenovo_component
from .firmware_version import FirmwareVersion
from .models import ComponentState, ServerProfile


class RedfishError(Exception):
    """Erreur lors d'un appel Redfish (HTTP >= 400, réponse invalide, etc.)."""


class RedfishAuthError(RedfishError):
    """Échec d'authentification Redfish (HTTP 401)."""


class RedfishClient:
    """Client Redfish minimal : session, GET, découverte Systems/Firmware.

    Un ``requests.Session`` peut être injecté (paramètre ``session``) pour
    permettre les tests sans matériel réel — voir ``test_redfish_client.py``
    qui utilise un faux transport plutôt que de contacter un vrai BMC.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
        session: requests.Session | None = None,
        timeout: float = 10.0,
    ) -> None:
        logger.debug(
            "RedfishClient.__init__() called | url={} utilisateur={} vérification_tls={}",
            base_url,
            username,
            verify_ssl,
        )
        self.base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._http = session or requests.Session()
        self._auth_token: str | None = None
        self._session_uri: str | None = None

        if not verify_ssl:
            # Réalité de terrain : la plupart des BMC (iLO/iDRAC/XCC) utilisent
            # par défaut un certificat auto-signé. On avertit explicitement
            # plutôt que de désactiver la vérification en silence.
            logger.warning(
                "Vérification TLS désactivée pour {} : certificat accepté "
                "sans validation. À réserver à un environnement de confiance.",
                self.base_url,
            )
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        logger.debug("RedfishClient.__init__() returning")

    # ------------------------------------------------------------------
    # Bas niveau
    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        logger.debug("_request() called | {} {}", method, path)
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        headers = kwargs.pop("headers", {}) or {}
        if self._auth_token:
            headers["X-Auth-Token"] = self._auth_token
        response = self._http.request(
            method, url, headers=headers, verify=self._verify_ssl, timeout=self._timeout, **kwargs
        )
        if response.status_code == 401:
            logger.error("authentification refusée : {} {}", method, url)
            raise RedfishAuthError(f"Authentification refusée sur {url}")
        if response.status_code >= 400:
            logger.error(
                "{} {} -> HTTP {} : {}",
                method,
                url,
                response.status_code,
                response.text[:200],
            )
            raise RedfishError(
                f"{method} {url} -> HTTP {response.status_code}: {response.text[:200]}"
            )
        logger.debug("_request() returning | HTTP {}", response.status_code)
        return response

    def get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path).json()

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------
    def connect(self) -> None:
        """Ouvre une session Redfish (``POST /redfish/v1/SessionService/Sessions``).

        Repli sur l'authentification HTTP Basic si le service de session
        échoue (certains BMC anciens/limités ne l'exposent pas, ou renvoient
        une erreur pour une autre raison) — HTTP Basic reste standard
        Redfish et fonctionne sur l'immense majorité des BMC.
        """
        logger.debug("connect() called")
        try:
            response = self._request(
                "POST",
                "/redfish/v1/SessionService/Sessions",
                json={"UserName": self._username, "Password": self._password},
            )
            self._auth_token = response.headers.get("X-Auth-Token")
            self._session_uri = response.headers.get("Location")
            if not self._auth_token:
                logger.error("session créée mais aucun X-Auth-Token reçu")
                raise RedfishError("Session créée mais aucun X-Auth-Token reçu.")
        except RedfishError as erreur:
            logger.info(
                "Service de session Redfish indisponible sur {} : repli sur HTTP Basic Auth ({})",
                self.base_url,
                erreur,
            )
            self._http.auth = (self._username, self._password)
        logger.debug("connect() returning")

    def close(self) -> None:
        """Ferme la session Redfish si une a été ouverte (best-effort)."""
        logger.debug("close() called | session={}", bool(self._session_uri))
        if self._session_uri:
            try:
                self._request("DELETE", self._session_uri)
            except RedfishError as erreur:
                # la session expirera d'elle-même côté BMC si la fermeture échoue
                logger.debug("fermeture de session best-effort ignorée : {}", erreur)
            self._auth_token = None
            self._session_uri = None
        logger.debug("close() returning")

    def __enter__(self) -> "RedfishClient":
        logger.debug("__enter__() called")
        self.connect()
        logger.debug("__enter__() returning")
        return self

    def __exit__(self, *exc: object) -> None:
        logger.debug("__exit__() called")
        self.close()
        logger.debug("__exit__() returning")

    # ------------------------------------------------------------------
    # Découverte
    # ------------------------------------------------------------------
    def get_service_root(self) -> dict[str, Any]:
        return self.get("/redfish/v1/")

    def list_system_paths(self) -> list[str]:
        logger.debug("list_system_paths() called")
        root = self.get("/redfish/v1/Systems")
        chemins = [m["@odata.id"] for m in root.get("Members", [])]
        logger.debug("list_system_paths() returning | {} système(s)", len(chemins))
        return chemins

    def list_manager_paths(self) -> list[str]:
        """Chemins des Managers (BMC) exposés (``/redfish/v1/Managers``).

        Nécessaire pour Virtual Media et NetworkProtocol/SNMP, qui sont des
        sous-ressources du Manager, pas du System.
        """
        logger.debug("list_manager_paths() called")
        root = self.get("/redfish/v1/Managers")
        chemins = [m["@odata.id"] for m in root.get("Members", [])]
        logger.debug("list_manager_paths() returning | {} manager(s)", len(chemins))
        return chemins

    def get_system(self, system_path: str) -> dict[str, Any]:
        return self.get(system_path)

    def list_firmware_inventory(self) -> list[dict[str, Any]]:
        """Retourne les entrées brutes de ``UpdateService/FirmwareInventory``.

        Le regroupement précis (BIOS/BMC/RAID/NIC...) varie selon le
        constructeur : on réutilise donc les classifieurs déjà écrits pour
        Dell et HPE (``catalog_import.py``) plutôt que d'inventer un mapping
        Redfish générique supplémentaire.
        """
        logger.debug("list_firmware_inventory() called")
        try:
            root = self.get("/redfish/v1/UpdateService/FirmwareInventory")
        except RedfishError as erreur:
            logger.debug("UpdateService absent, inventaire vide : {}", erreur)
            return []  # UpdateService absent/non standard sur ce BMC

        entries = []
        for member in root.get("Members", []):
            try:
                entries.append(self.get(member["@odata.id"]))
            except RedfishError as erreur:
                logger.debug("entrée d'inventaire ignorée : {}", erreur)
                continue  # une entrée illisible ne doit pas bloquer les autres
        logger.debug("list_firmware_inventory() returning | {} entrée(s)", len(entries))
        return entries

    # ------------------------------------------------------------------
    # Alimentation (Actions/ComputerSystem.Reset)
    # ------------------------------------------------------------------
    #: Types de reset définis par le schéma Redfish standard (DMTF). Un BMC
    #: donné n'en supporte généralement qu'un sous-ensemble — voir
    #: ``allowable_reset_types``.
    STANDARD_RESET_TYPES = frozenset(
        {
            "On",
            "ForceOn",
            "ForceOff",
            "GracefulShutdown",
            "GracefulRestart",
            "ForceRestart",
            "PushPowerButton",
            "Nmi",
        }
    )

    def get_power_state(self, system_path: str) -> str:
        """Retourne l'état d'alimentation courant (``On``/``Off``/...)."""
        logger.debug("get_power_state() called | système={}", system_path)
        system = self.get_system(system_path)
        power_state = system.get("PowerState")
        if not power_state:
            logger.error("champ PowerState absent sur {}", system_path)
            raise RedfishError(f"Champ PowerState absent sur {system_path}")
        logger.debug("get_power_state() returning | {}", power_state)
        return power_state

    def _reset_action_target(self, system: dict[str, Any], system_path: str) -> str:
        """Trouve l'URI cible de l'action Reset.

        Le schéma Redfish laisse cette cible dynamique (``Actions``), mais la
        quasi-totalité des BMC utilisent le chemin conventionnel ci-dessous
        s'il n'est pas explicitement fourni.
        """
        logger.debug("_reset_action_target() called | système={}", system_path)
        actions = system.get("Actions", {})
        reset_action = actions.get("#ComputerSystem.Reset", {})
        target = reset_action.get("target")
        cible = target or f"{system_path}/Actions/ComputerSystem.Reset"
        logger.debug("_reset_action_target() returning | {}", cible)
        return cible

    def allowable_reset_types(self, system_path: str) -> set[str]:
        """Types de reset réellement supportés par ce système, si annoncés.

        Repli sur ``STANDARD_RESET_TYPES`` si le BMC n'annonce pas
        ``ResetType@Redfish.AllowableValues`` (certains BMC anciens/limités).
        """
        logger.debug("allowable_reset_types() called | système={}", system_path)
        system = self.get_system(system_path)
        reset_action = system.get("Actions", {}).get("#ComputerSystem.Reset", {})
        allowable = reset_action.get("ResetType@Redfish.AllowableValues")
        autorises = set(allowable) if allowable else set(self.STANDARD_RESET_TYPES)
        logger.debug("allowable_reset_types() returning | {} type(s)", len(autorises))
        return autorises

    def reset_system(self, system_path: str, reset_type: str) -> None:
        """Envoie une action de reset (power on/off/reboot/NMI...).

        Args:
            system_path: chemin du système (ex. ``/redfish/v1/Systems/1``).
            reset_type: une valeur Redfish standard (ex. ``"On"``,
                ``"GracefulShutdown"``, ``"ForceRestart"``). Vérifiée contre
                ``allowable_reset_types`` avant l'envoi — on ne laisse jamais
                partir une action non annoncée comme supportée, plutôt que de
                laisser le BMC échouer silencieusement ou pire.
        """
        logger.debug("reset_system() called | système={} type={}", system_path, reset_type)
        system = self.get_system(system_path)
        allowed = self.allowable_reset_types(system_path)
        if reset_type not in allowed:
            logger.error(
                "ResetType {} non supporté (autorisés : {})",
                reset_type,
                sorted(allowed),
            )
            raise RedfishError(
                f"ResetType {reset_type!r} non supporté par ce système "
                f"(valeurs autorisées : {sorted(allowed)})"
            )
        target = self._reset_action_target(system, system_path)
        self._request("POST", target, json={"ResetType": reset_type})
        logger.debug("reset_system() returning")

    def power_on(self, system_path: str) -> None:
        logger.debug("power_on() called | système={}", system_path)
        self.reset_system(system_path, "On")
        logger.debug("power_on() returning")

    def power_off_graceful(self, system_path: str) -> None:
        logger.debug("power_off_graceful() called | système={}", system_path)
        self.reset_system(system_path, "GracefulShutdown")
        logger.debug("power_off_graceful() returning")

    def power_off_force(self, system_path: str) -> None:
        logger.debug("power_off_force() called | système={}", system_path)
        self.reset_system(system_path, "ForceOff")
        logger.debug("power_off_force() returning")

    def reboot_graceful(self, system_path: str) -> None:
        logger.debug("reboot_graceful() called | système={}", system_path)
        self.reset_system(system_path, "GracefulRestart")
        logger.debug("reboot_graceful() returning")

    def reboot_force(self, system_path: str) -> None:
        logger.debug("reboot_force() called | système={}", system_path)
        self.reset_system(system_path, "ForceRestart")
        logger.debug("reboot_force() returning")

    def send_nmi(self, system_path: str) -> None:
        """Envoie un NMI (diagnostic bas niveau — provoque un crash dump)."""
        logger.debug("send_nmi() called | système={}", system_path)
        self.reset_system(system_path, "Nmi")
        logger.debug("send_nmi() returning")

    # ------------------------------------------------------------------
    # Réseau / SNMP (ManagerNetworkProtocol — standard DMTF)
    # ------------------------------------------------------------------
    def get_network_protocol(self, manager_path: str) -> dict[str, Any]:
        return self.get(f"{manager_path}/NetworkProtocol")

    def set_snmp_agent(self, manager_path: str, enabled: bool, port: int | None = None) -> None:
        """Active/désactive l'agent SNMP et modifie son port d'écoute.

        Standard DMTF (``ManagerNetworkProtocol.SNMP``), confirmé sur HPE
        iLO. Ne configure PAS les destinations de trap ni les community
        strings d'alerte — voir ``create_snmp_trap_destination`` pour ça
        (mécanisme séparé, ``EventService/Subscriptions``).
        """
        logger.debug(
            "set_snmp_agent() called | manager={} activé={}", manager_path, enabled
        )
        body: dict[str, Any] = {"SNMP": {"ProtocolEnabled": enabled}}
        if port is not None:
            body["SNMP"]["Port"] = port
        self._request("PATCH", f"{manager_path}/NetworkProtocol", json=body)
        logger.debug("set_snmp_agent() returning")

    # ------------------------------------------------------------------
    # Abonnements d'événements : SNMP traps et Syslog (EventService)
    # ------------------------------------------------------------------
    #: Protocoles SNMP trap valides (ConnectionMethodType Redfish récent :
    #: "SNMP" seul est déprécié au profit de ces trois valeurs versionnées).
    SNMP_TRAP_PROTOCOLS = frozenset({"SNMPv1", "SNMPv2c", "SNMPv3"})
    #: Protocoles Syslog valides, confirmés sur HPE iLO 6 (>= 1.68). Support
    #: Dell/Lenovo non vérifié ici — à tester avant usage en production.
    SYSLOG_PROTOCOLS = frozenset({"SyslogUDP", "SyslogTLS"})

    def list_event_subscriptions(self) -> list[dict[str, Any]]:
        logger.debug("list_event_subscriptions() called")
        try:
            root = self.get("/redfish/v1/EventService/Subscriptions")
        except RedfishError as erreur:
            logger.debug("EventService absent, aucun abonnement : {}", erreur)
            return []  # EventService absent sur ce BMC
        abonnements = [self.get(m["@odata.id"]) for m in root.get("Members", [])]
        logger.debug("list_event_subscriptions() returning | {} abonnement(s)", len(abonnements))
        return abonnements

    def create_snmp_trap_destination(
        self,
        destination_host: str,
        community: str | None = None,
        protocol: str = "SNMPv2c",
        port: int = 162,
    ) -> str:
        """Ajoute une destination de trap SNMP.

        Standardisé par Redfish depuis la révision 2019.3 (mise à jour des
        schémas EventDestination/EventService/ManagerNetworkProtocol) ;
        confirmé fonctionnel sur Lenovo XCC et HPE iLO. La gestion fine
        SNMPv3 (utilisateur/auth/priv) varie selon le constructeur et n'est
        pas couverte ici.

        Returns:
            L'URI de l'abonnement créé (en-tête ``Location``), si fourni.
        """
        logger.debug(
            "create_snmp_trap_destination() called | hôte={} protocole={}",
            destination_host,
            protocol,
        )
        if protocol not in self.SNMP_TRAP_PROTOCOLS:
            logger.error("protocole SNMP inconnu : {}", protocol)
            raise RedfishError(
                f"Protocole SNMP inconnu : {protocol!r} (attendu l'un de "
                f"{sorted(self.SNMP_TRAP_PROTOCOLS)})"
            )
        body: dict[str, Any] = {
            "Destination": f"snmp://{destination_host}:{port}",
            "Protocol": protocol,
            "SubscriptionType": "SNMPTrap",
        }
        if community:
            body["SNMP"] = {"TrapCommunity": community}
        response = self._request("POST", "/redfish/v1/EventService/Subscriptions", json=body)
        uri = response.headers.get("Location", "")
        logger.debug("create_snmp_trap_destination() returning | {}", uri)
        return uri

    def create_syslog_destination(
        self, destination_host: str, port: int = 514, protocol: str = "SyslogUDP"
    ) -> str:
        """Ajoute une destination syslog distante.

        Confirmé disponible sur HPE iLO 6 (>= 1.68). Support Dell/Lenovo non
        vérifié à ce stade — à tester avant usage en production sur ces
        constructeurs plutôt que supposé équivalent.
        """
        logger.debug(
            "create_syslog_destination() called | hôte={} protocole={}",
            destination_host,
            protocol,
        )
        if protocol not in self.SYSLOG_PROTOCOLS:
            logger.error("protocole Syslog inconnu : {}", protocol)
            raise RedfishError(
                f"Protocole Syslog inconnu : {protocol!r} (attendu l'un de "
                f"{sorted(self.SYSLOG_PROTOCOLS)})"
            )
        body = {
            "Destination": f"syslog://{destination_host}:{port}",
            "Protocol": protocol,
            "SubscriptionType": "Syslog",
        }
        response = self._request("POST", "/redfish/v1/EventService/Subscriptions", json=body)
        uri = response.headers.get("Location", "")
        logger.debug("create_syslog_destination() returning | {}", uri)
        return uri

    def delete_event_subscription(self, subscription_path: str) -> None:
        logger.debug("delete_event_subscription() called | {}", subscription_path)
        self._request("DELETE", subscription_path)
        logger.debug("delete_event_subscription() returning")

    # ------------------------------------------------------------------
    # RAID (Storage — LECTURE SEULE)
    # ------------------------------------------------------------------
    # Décision de projet : la création/suppression de volumes RAID via
    # Redfish a été retirée volontairement. Le risque (perte de données en
    # cas de mauvaise cible/mauvais niveau, support très inégal selon
    # constructeur — confirmé absent sur Lenovo XCC par exemple) dépasse la
    # valeur ajoutée pour ce projet. On ne garde que la consultation.
    def list_storage_controllers(self, system_path: str) -> list[str]:
        logger.debug("list_storage_controllers() called | système={}", system_path)
        root = self.get(f"{system_path}/Storage")
        chemins = [m["@odata.id"] for m in root.get("Members", [])]
        logger.debug("list_storage_controllers() returning | {} contrôleur(s)", len(chemins))
        return chemins

    def list_drives(self, controller_path: str) -> list[str]:
        logger.debug("list_drives() called | contrôleur={}", controller_path)
        controller = self.get(controller_path)
        chemins = [d["@odata.id"] for d in controller.get("Drives", [])]
        logger.debug("list_drives() returning | {} disque(s)", len(chemins))
        return chemins

    def list_volumes(self, controller_path: str) -> list[dict[str, Any]]:
        """Retourne les volumes RAID existants (lecture seule)."""
        logger.debug("list_volumes() called | contrôleur={}", controller_path)
        try:
            root = self.get(f"{controller_path}/Volumes")
        except RedfishError as erreur:
            logger.debug("collection Volumes absente : {}", erreur)
            return []  # pas de collection Volumes exposée sur ce contrôleur
        volumes = [self.get(m["@odata.id"]) for m in root.get("Members", [])]
        logger.debug("list_volumes() returning | {} volume(s)", len(volumes))
        return volumes

    # ------------------------------------------------------------------
    # Virtual Media (monter/démonter une ISO distante)
    # ------------------------------------------------------------------
    def list_virtual_media(self, manager_path: str) -> list[dict[str, Any]]:
        """Retourne les lecteurs Virtual Media disponibles (état inclus)."""
        logger.debug("list_virtual_media() called | manager={}", manager_path)
        root = self.get(f"{manager_path}/VirtualMedia")
        medias = [self.get(m["@odata.id"]) for m in root.get("Members", [])]
        logger.debug("list_virtual_media() returning | {} lecteur(s)", len(medias))
        return medias

    def _virtual_media_action_target(
        self, media: dict[str, Any], media_path: str, action_name: str
    ) -> str:
        """Trouve l'URI cible de l'action Insert/EjectMedia.

        Comme pour Reset, la cible est en théorie dynamique (``Actions``)
        mais la quasi-totalité des BMC suit le chemin conventionnel si elle
        n'est pas explicitement fournie.
        """
        logger.debug("_virtual_media_action_target() called | action={}", action_name)
        actions = media.get("Actions", {})
        action = actions.get(f"#VirtualMedia.{action_name}", {})
        cible = action.get("target") or f"{media_path}/Actions/VirtualMedia.{action_name}"
        logger.debug("_virtual_media_action_target() returning | {}", cible)
        return cible

    def insert_media(
        self,
        media_path: str,
        image_url: str,
        write_protected: bool = True,
    ) -> None:
        """Monte une image ISO/IMG distante sur un lecteur Virtual Media.

        Args:
            media_path: chemin du lecteur (ex.
                ``/redfish/v1/Managers/1/VirtualMedia/CD``).
            image_url: URL de l'image accessible par le BMC (HTTP/HTTPS/NFS/
                CIFS selon constructeur — non validée ici, le BMC renverra
                une erreur si le protocole ou le chemin n'est pas atteignable
                depuis son propre réseau, ce qui diffère souvent du réseau
                du poste qui lance ce client).
            write_protected: monte l'image en lecture seule (recommandé pour
                une ISO d'installation).
        """
        logger.debug(
            "insert_media() called | lecteur={} image={}", media_path, image_url
        )
        media = self.get(media_path)
        target = self._virtual_media_action_target(media, media_path, "InsertMedia")
        self._request(
            "POST",
            target,
            json={"Image": image_url, "Inserted": True, "WriteProtected": write_protected},
        )
        logger.debug("insert_media() returning")

    def eject_media(self, media_path: str) -> None:
        """Démonte l'image actuellement montée sur ce lecteur Virtual Media."""
        logger.debug("eject_media() called | lecteur={}", media_path)
        media = self.get(media_path)
        target = self._virtual_media_action_target(media, media_path, "EjectMedia")
        self._request("POST", target, json={})
        logger.debug("eject_media() returning")

    # ------------------------------------------------------------------
    # Santé et capteurs (Chassis/Thermal, Chassis/Power — lecture seule)
    # ------------------------------------------------------------------
    def list_chassis_paths(self) -> list[str]:
        logger.debug("list_chassis_paths() called")
        root = self.get("/redfish/v1/Chassis")
        chemins = [m["@odata.id"] for m in root.get("Members", [])]
        logger.debug("list_chassis_paths() returning | {} châssis", len(chemins))
        return chemins

    def get_system_health(self, system_path: str) -> dict[str, Any]:
        """Retourne le résumé de santé (``Status.Health``/``HealthRollup``)."""
        logger.debug("get_system_health() called | système={}", system_path)
        system = self.get_system(system_path)
        status = system.get("Status", {})
        logger.debug("get_system_health() returning | état={}", status.get("Health"))
        return status

    def get_thermal(self, chassis_path: str) -> dict[str, Any]:
        """Températures et ventilateurs (schéma ``Thermal``, le plus répandu
        en pratique). Le schéma plus récent ``ThermalSubsystem`` (Redfish
        >= 2021.1) n'est pas couvert ici — à ajouter si un BMC de test ne
        publie que celui-ci."""
        logger.debug("get_thermal() called | châssis={}", chassis_path)
        try:
            thermique = self.get(f"{chassis_path}/Thermal")
            logger.debug("get_thermal() returning | {} entrée(s)", len(thermique))
            return thermique
        except RedfishError as erreur:
            logger.debug("schéma Thermal absent : {}", erreur)
            return {}

    def get_power_readings(self, chassis_path: str) -> dict[str, Any]:
        """Alimentations et consommation (schéma ``Power``, le plus répandu
        en pratique). Le schéma plus récent ``PowerSubsystem`` n'est pas
        couvert ici — même remarque que pour ``get_thermal``."""
        logger.debug("get_power_readings() called | châssis={}", chassis_path)
        try:
            puissance = self.get(f"{chassis_path}/Power")
            logger.debug("get_power_readings() returning | {} entrée(s)", len(puissance))
            return puissance
        except RedfishError as erreur:
            logger.debug("schéma Power absent : {}", erreur)
            return {}

    # ------------------------------------------------------------------
    # Logs matériels (LogServices — lecture seule)
    # ------------------------------------------------------------------
    def list_log_services(self, owner_path: str) -> list[str]:
        """``owner_path`` : un chemin System ou Manager (les deux peuvent
        exposer des ``LogServices`` distincts — évènements système vs
        évènements du BMC lui-même)."""
        logger.debug("list_log_services() called | propriétaire={}", owner_path)
        try:
            root = self.get(f"{owner_path}/LogServices")
        except RedfishError as erreur:
            logger.debug("LogServices absent : {}", erreur)
            return []
        chemins = [m["@odata.id"] for m in root.get("Members", [])]
        logger.debug("list_log_services() returning | {} service(s)", len(chemins))
        return chemins

    def list_log_entries(self, log_service_path: str, limit: int | None = 100) -> list[dict[str, Any]]:
        """Retourne les entrées d'un journal (le plus souvent le SEL).

        Args:
            limit: nombre maximal d'entrées retournées (les journaux
                peuvent en contenir plusieurs milliers) ; ``None`` pour tout
                récupérer.
        """
        logger.debug("list_log_entries() called | service={}", log_service_path)
        try:
            root = self.get(f"{log_service_path}/Entries")
        except RedfishError as erreur:
            logger.debug("journal illisible : {}", erreur)
            return []
        members = root.get("Members", [])
        if limit is not None:
            members = members[:limit]
        logger.debug("list_log_entries() returning | {} entrée(s)", len(members))
        return members  # les entrées de log sont généralement inline, pas des références

    # ------------------------------------------------------------------
    # Suivi de tâches asynchrones (TaskService — lecture seule)
    # ------------------------------------------------------------------
    def list_tasks(self) -> list[dict[str, Any]]:
        logger.debug("list_tasks() called")
        try:
            root = self.get("/redfish/v1/TaskService/Tasks")
        except RedfishError as erreur:
            logger.debug("TaskService absent : {}", erreur)
            return []
        taches = [self.get(m["@odata.id"]) for m in root.get("Members", [])]
        logger.debug("list_tasks() returning | {} tâche(s)", len(taches))
        return taches

    def get_task(self, task_path: str) -> dict[str, Any]:
        return self.get(task_path)

    # ------------------------------------------------------------------
    # Boot one-shot (PXE/CD au prochain démarrage uniquement)
    # ------------------------------------------------------------------
    def get_boot_options(self, system_path: str) -> dict[str, Any]:
        """Retourne la configuration Boot courante, avec les cibles
        autorisées (``BootSourceOverrideTarget@Redfish.AllowableValues``)."""
        logger.debug("get_boot_options() called | système={}", system_path)
        system = self.get_system(system_path)
        boot = system.get("Boot", {})
        logger.debug("get_boot_options() returning | {} cible(s)", len(boot.get("BootSourceOverrideTarget@Redfish.AllowableValues", [])))
        return boot

    def set_boot_override_once(self, system_path: str, target: str) -> None:
        """Force la prochaine (et uniquement la prochaine) séquence de boot.

        Args:
            target: une valeur Redfish standard (ex. ``"Pxe"``, ``"Cd"``,
                ``"UsbExternal"``, ``"BiosSetup"``). Vérifiée contre les
                valeurs annoncées par ce système avant l'envoi — même
                principe de sécurité que pour ``reset_system``.

        Ce réglage est du "one-shot" (``BootSourceOverrideEnabled: "Once"``)
        : il s'applique une seule fois puis le système revient à sa
        séquence normale, contrairement à un réglage permanent — délibéré,
        pour limiter le risque d'un serveur qui boot indéfiniment sur PXE
        par erreur de configuration oubliée.
        """
        logger.debug("set_boot_override_once() called | système={} cible={}", system_path, target)
        boot = self.get_boot_options(system_path)
        allowed = boot.get("BootSourceOverrideTarget@Redfish.AllowableValues")
        if allowed and target not in allowed:
            logger.error("cible de boot {} non supportée (autorisées : {})", target, sorted(allowed))
            raise RedfishError(
                f"Cible de boot {target!r} non supportée par ce système "
                f"(valeurs autorisées : {sorted(allowed)})"
            )
        self._request(
            "PATCH",
            system_path,
            json={"Boot": {"BootSourceOverrideEnabled": "Once", "BootSourceOverrideTarget": target}},
        )
        logger.debug("set_boot_override_once() returning")

    def clear_boot_override(self, system_path: str) -> None:
        """Annule un override de boot programmé (repasse à ``Disabled``)."""
        logger.debug("clear_boot_override() called | système={}", system_path)
        self._request(
            "PATCH", system_path, json={"Boot": {"BootSourceOverrideEnabled": "Disabled"}}
        )
        logger.debug("clear_boot_override() returning")

    # ------------------------------------------------------------------
    # Console (statut uniquement — Redfish ne standardise pas le protocole
    # de console lui-même, propriétaire à chaque constructeur)
    # ------------------------------------------------------------------
    def get_console_info(self, manager_path: str) -> dict[str, Any]:
        """Retourne les infos de disponibilité console (``GraphicalConsole``/
        ``SerialConsole`` du Manager) : activé, sessions max/courantes,
        types de connexion supportés. Ne donne PAS d'URL de connexion ni de
        protocole exploitable directement — pour la console graphique, la
        page web native du BMC (``https://<hostname>/``) reste le point
        d'entrée standard ; pour la série, voir ``ConnectTypesSupported``
        (souvent SSH, auquel cas un plugin SSH existant peut être réutilisé
        tel quel plutôt que de réimplémenter un protocole propriétaire)."""
        logger.debug("get_console_info() called | manager={}", manager_path)
        manager = self.get(manager_path)
        infos = {
            "GraphicalConsole": manager.get("GraphicalConsole", {}),
            "SerialConsole": manager.get("SerialConsole", {}),
        }
        logger.debug("get_console_info() returning")
        return infos

    # ------------------------------------------------------------------
    # LED d'identification (IndicatorLED — Chassis ou Systems)
    # ------------------------------------------------------------------
    #: Valeurs standard DMTF pour ``IndicatorLED``. "Unknown" est une valeur
    #: de lecture (état non déterminable), jamais un état qu'on demande.
    INDICATOR_LED_STATES = frozenset({"Lit", "Blinking", "Off"})

    def get_indicator_led(self, resource_path: str) -> str:
        """Lit l'état de la LED d'identification (Chassis ou System)."""
        logger.debug("get_indicator_led() called | ressource={}", resource_path)
        resource = self.get(resource_path)
        etat = resource.get("IndicatorLED", "Unknown")
        logger.debug("get_indicator_led() returning | {}", etat)
        return etat

    def set_indicator_led(self, resource_path: str, state: str) -> None:
        """Change l'état de la LED d'identification (utile pour repérer un
        serveur physiquement dans une baie)."""
        logger.debug("set_indicator_led() called | ressource={} état={}", resource_path, state)
        if state not in self.INDICATOR_LED_STATES:
            logger.error("état IndicatorLED {} invalide", state)
            raise RedfishError(
                f"État IndicatorLED {state!r} invalide (attendu l'un de "
                f"{sorted(self.INDICATOR_LED_STATES)})"
            )
        self._request("PATCH", resource_path, json={"IndicatorLED": state})
        logger.debug("set_indicator_led() returning")

    # ------------------------------------------------------------------
    # Sessions actives (SessionService — lecture seule)
    # ------------------------------------------------------------------
    def list_active_sessions(self) -> list[dict[str, Any]]:
        """Retourne les sessions Redfish actuellement ouvertes sur ce BMC
        (qui est connecté, depuis quand, avec quel rôle) — utile pour
        détecter un accès non attendu."""
        logger.debug("list_active_sessions() called")
        try:
            root = self.get("/redfish/v1/SessionService/Sessions")
        except RedfishError as erreur:
            logger.debug("SessionService illisible : {}", erreur)
            return []
        sessions = [self.get(m["@odata.id"]) for m in root.get("Members", [])]
        logger.debug("list_active_sessions() returning | {} session(s)", len(sessions))
        return sessions

    # ------------------------------------------------------------------
    # Date/heure et NTP (Manager + ManagerNetworkProtocol.NTP)
    # ------------------------------------------------------------------
    def get_datetime(self, manager_path: str) -> dict[str, Any]:
        logger.debug("get_datetime() called | manager={}", manager_path)
        manager = self.get(manager_path)
        horodatage = {
            "DateTime": manager.get("DateTime"),
            "DateTimeLocalOffset": manager.get("DateTimeLocalOffset"),
        }
        logger.debug("get_datetime() returning | {}", horodatage.get("DateTime"))
        return horodatage

    def set_datetime(
        self, manager_path: str, date_time_iso: str, local_offset: str | None = None
    ) -> None:
        """Règle la date/heure manuellement (ignoré si NTP est actif sur la
        plupart des BMC — cohérence des horodatages de logs à vérifier après
        coup plutôt que supposée)."""
        logger.debug("set_datetime() called | manager={}", manager_path)
        body: dict[str, Any] = {"DateTime": date_time_iso}
        if local_offset:
            body["DateTimeLocalOffset"] = local_offset
        self._request("PATCH", manager_path, json=body)
        logger.debug("set_datetime() returning")

    def get_ntp_settings(self, manager_path: str) -> dict[str, Any]:
        logger.debug("get_ntp_settings() called | manager={}", manager_path)
        protocol = self.get_network_protocol(manager_path)
        ntp = protocol.get("NTP", {})
        logger.debug("get_ntp_settings() returning | {} serveur(s)", len(ntp.get("NTPServers", [])))
        return ntp

    def set_ntp_servers(self, manager_path: str, servers: list[str], enabled: bool = True) -> None:
        logger.debug(
            "set_ntp_servers() called | manager={} {} serveur(s) activé={}",
            manager_path,
            len(servers),
            enabled,
        )
        self._request(
            "PATCH",
            f"{manager_path}/NetworkProtocol",
            json={"NTP": {"ProtocolEnabled": enabled, "NTPServers": servers}},
        )
        logger.debug("set_ntp_servers() returning")

    # ------------------------------------------------------------------
    # Licence (LicenseService — lecture seule ; schéma Redfish récent,
    # pas universellement présent selon l'âge du firmware)
    # ------------------------------------------------------------------
    def list_licenses(self) -> list[dict[str, Any]]:
        """Licences installées (ex. iLO Advanced, iDRAC Enterprise, XCC Pro),
        utile pour savoir si une fonctionnalité (Virtual Media avancé,
        SNMPv3...) est réellement disponible sur ce BMC précis."""
        logger.debug("list_licenses() called")
        try:
            root = self.get("/redfish/v1/LicenseService/Licenses")
        except RedfishError as erreur:
            logger.debug("LicenseService absent : {}", erreur)
            return []  # LicenseService absent : firmware ancien ou constructeur différent
        licences = [self.get(m["@odata.id"]) for m in root.get("Members", [])]
        logger.debug("list_licenses() returning | {} licence(s)", len(licences))
        return licences

    # ------------------------------------------------------------------
    # Mise à jour firmware d'un composant (UpdateService.SimpleUpdate)
    # ------------------------------------------------------------------
    # Point important : ceci flashe UN fichier sur UNE cible, sans aucun
    # calcul de palier/dépendance (cette partie a été volontairement
    # abandonnée — voir le reste du projet). L'appelant reste responsable
    # de l'ordre et de la pertinence de ce qu'il envoie.
    SIMPLE_UPDATE_PROTOCOLS = frozenset(
        {"HTTP", "HTTPS", "FTP", "SFTP", "SCP", "TFTP", "NFS", "CIFS"}
    )

    def simple_update(
        self,
        image_uri: str,
        targets: list[str] | None = None,
        transfer_protocol: str = "HTTP",
    ) -> str:
        """Déclenche le flash d'un firmware (``UpdateService.SimpleUpdate``).

        Args:
            image_uri: URI de l'image, accessible depuis le réseau du BMC
                (même remarque que pour Virtual Media : pas depuis le poste
                qui lance ce client).
            targets: URIs des ``FirmwareInventory`` ciblées ; ``None`` pour
                laisser le BMC déterminer la cible à partir du contenu de
                l'image (comportement par défaut chez la plupart des BMC).
            transfer_protocol: protocole de récupération de l'image par le
                BMC (pas le protocole entre ce client et le BMC).

        Returns:
            L'URI de la tâche de suivi (``Location``), à passer à
            ``get_task`` pour suivre la progression — cette opération est
            presque toujours asynchrone.
        """
        logger.debug(
            "simple_update() called | image={} protocole={} cible(s)={}",
            image_uri,
            transfer_protocol,
            len(targets or []),
        )
        if transfer_protocol not in self.SIMPLE_UPDATE_PROTOCOLS:
            logger.error("protocole de transfert {} invalide", transfer_protocol)
            raise RedfishError(
                f"Protocole de transfert {transfer_protocol!r} invalide "
                f"(attendu l'un de {sorted(self.SIMPLE_UPDATE_PROTOCOLS)})"
            )
        body: dict[str, Any] = {"ImageURI": image_uri, "TransferProtocol": transfer_protocol}
        if targets:
            body["Targets"] = targets
        response = self._request(
            "POST", "/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate", json=body
        )
        uri = response.headers.get("Location", "")
        logger.debug("simple_update() returning | tâche={}", uri)
        return uri

    # ------------------------------------------------------------------
    # BIOS (Systems/{id}/Bios — via le mécanisme standard @Redfish.Settings)
    # ------------------------------------------------------------------
    # Point de sécurité important, différent des autres actions de ce
    # fichier : contrairement à ResetType/IndicatorLED, il n'existe pas de
    # registre universel des valeurs autorisées par attribut BIOS (chaque
    # constructeur, voire chaque modèle, a son propre "BIOS Attribute
    # Registry"). On ne valide donc PAS les valeurs ici — l'appelant doit
    # connaître les attributs qu'il modifie. On applique en revanche
    # systématiquement le mécanisme "Settings" (changement en attente,
    # appliqué au prochain redémarrage) plutôt qu'un PATCH direct sur la
    # ressource Bios live, qui est généralement refusé ou sans effet sur la
    # plupart des BMC.
    def get_bios_attributes(self, system_path: str) -> dict[str, Any]:
        """Retourne les attributs BIOS actuellement actifs."""
        logger.debug("get_bios_attributes() called | système={}", system_path)
        bios = self.get(f"{system_path}/Bios")
        attributs = bios.get("Attributes", {})
        logger.debug("get_bios_attributes() returning | {} attribut(s)", len(attributs))
        return attributs

    def _bios_settings_target(self, system_path: str) -> str:
        logger.debug("_bios_settings_target() called | système={}", system_path)
        bios = self.get(f"{system_path}/Bios")
        settings = bios.get("@Redfish.Settings", {}).get("SettingsObject", {})
        target = settings.get("@odata.id")
        if not target:
            logger.error("aucun objet Settings BIOS trouvé pour {}", system_path)
            raise RedfishError(
                f"Aucun objet Settings BIOS trouvé pour {system_path} : "
                "ce BMC n'expose peut-être pas le mécanisme standard "
                "@Redfish.Settings pour son BIOS."
            )
        logger.debug("_bios_settings_target() returning | {}", target)
        return target

    def get_bios_pending_attributes(self, system_path: str) -> dict[str, Any]:
        """Retourne les changements BIOS en attente (pas encore appliqués)."""
        logger.debug("get_bios_pending_attributes() called | système={}", system_path)
        target = self._bios_settings_target(system_path)
        attributs = self.get(target).get("Attributes", {})
        logger.debug("get_bios_pending_attributes() returning | {} attribut(s)", len(attributs))
        return attributs

    def set_bios_attributes(self, system_path: str, attributes: dict[str, Any]) -> None:
        """Programme un changement d'attributs BIOS, appliqué au **prochain
        redémarrage** (mécanisme standard ``@Redfish.Settings``, jamais un
        changement immédiat).

        Aucune validation de valeur n'est faite ici (voir note plus haut) :
        un attribut ou une valeur incorrecte peut être refusé par le BMC, ou
        pire, accepté mais gêner le redémarrage suivant. À utiliser avec des
        attributs dont la validité a été vérifiée par ailleurs (registre
        BIOS du constructeur, ou test préalable).
        """
        logger.debug(
            "set_bios_attributes() called | système={} {} attribut(s)",
            system_path,
            len(attributes),
        )
        target = self._bios_settings_target(system_path)
        self._request("PATCH", target, json={"Attributes": attributes})
        logger.debug("set_bios_attributes() returning")

    # ------------------------------------------------------------------
    # Réseau du Manager/BMC (EthernetInterfaces — standard DMTF)
    # ------------------------------------------------------------------
    # Point important à connaître avant usage : le concept de port "dédié"
    # vs "partagé" (le BMC utilise son propre port physique ou partage un
    # port avec les NIC du serveur) N'EST PAS standardisé par le schéma
    # Redfish EthernetInterface — chaque constructeur le gère via ses
    # propres attributs (ex. OEM Dell/HPE). Cette section couvre donc
    # uniquement ce qui EST standard (adressage IP, VLAN, activation d'une
    # interface) ; le bascule dédié/partagé n'est pas implémenté ici faute
    # de champ standard confirmé — à traiter constructeur par constructeur
    # si besoin, plutôt que de deviner un nom d'attribut OEM.
    def list_manager_ethernet_interfaces(self, manager_path: str) -> list[str]:
        logger.debug("list_manager_ethernet_interfaces() called | manager={}", manager_path)
        root = self.get(f"{manager_path}/EthernetInterfaces")
        chemins = [m["@odata.id"] for m in root.get("Members", [])]
        logger.debug("list_manager_ethernet_interfaces() returning | {} interface(s)", len(chemins))
        return chemins

    def get_ethernet_interface(self, interface_path: str) -> dict[str, Any]:
        return self.get(interface_path)

    def set_ethernet_interface(
        self,
        interface_path: str,
        dhcp_enabled: bool | None = None,
        static_ipv4: dict[str, str] | None = None,
        vlan_id: int | None = None,
    ) -> None:
        """Modifie l'adressage IP/VLAN d'une interface réseau du Manager.

        Args:
            static_ipv4: dict avec les clés ``Address``, ``SubnetMask``,
                ``Gateway`` (norme ``IPv4Addresses`` Redfish). Ignoré si
                ``dhcp_enabled`` est vrai.
            vlan_id: identifiant VLAN, si l'interface le supporte.

        Attention : un mauvais réglage ici peut couper l'accès réseau au
        BMC lui-même (pas de session de rattrapage possible à distance dans
        ce cas — accès physique/série nécessaire).
        """
        logger.debug("set_ethernet_interface() called | interface={}", interface_path)
        body: dict[str, Any] = {}
        if dhcp_enabled is not None:
            body["DHCPv4"] = {"DHCPEnabled": dhcp_enabled}
        if static_ipv4 is not None and not dhcp_enabled:
            body["IPv4Addresses"] = [static_ipv4]
        if vlan_id is not None:
            body["VLAN"] = {"VLANEnable": True, "VLANId": vlan_id}
        if not body:
            logger.error("aucun changement fourni à set_ethernet_interface")
            raise RedfishError("Aucun changement fourni à set_ethernet_interface.")
        self._request("PATCH", interface_path, json=body)
        logger.debug("set_ethernet_interface() returning")

    # ------------------------------------------------------------------
    # Certificats TLS (CertificateService — standard DMTF depuis ~2019)
    # ------------------------------------------------------------------
    def list_certificates(self, certificate_collection_path: str) -> list[dict[str, Any]]:
        """Liste les certificats d'une collection donnée (typiquement
        ``Managers/{id}/NetworkProtocol/HTTPS/Certificates``)."""
        logger.debug("list_certificates() called | collection={}", certificate_collection_path)
        try:
            root = self.get(certificate_collection_path)
        except RedfishError as erreur:
            logger.debug("collection de certificats illisible : {}", erreur)
            return []
        certificats = [self.get(m["@odata.id"]) for m in root.get("Members", [])]
        logger.debug("list_certificates() returning | {} certificat(s)", len(certificats))
        return certificats

    def generate_csr(
        self,
        certificate_collection_path: str,
        common_name: str,
        organization: str,
        country: str,
        additional_fields: dict[str, str] | None = None,
    ) -> str:
        """Génère une CSR (Certificate Signing Request) via
        ``CertificateService.GenerateCSR``.

        Returns:
            Le contenu de la CSR (PEM), à faire signer par une autorité
            avant de revenir avec ``replace_certificate``.
        """
        logger.debug(
            "generate_csr() called | collection={} nom={}",
            certificate_collection_path,
            common_name,
        )
        body: dict[str, Any] = {
            "CertificateCollection": {"@odata.id": certificate_collection_path},
            "CommonName": common_name,
            "Organization": organization,
            "Country": country,
        }
        if additional_fields:
            body.update(additional_fields)
        response = self._request(
            "POST", "/redfish/v1/CertificateService/Actions/CertificateService.GenerateCSR", json=body
        )
        csr = response.json().get("CSRString", "")
        logger.debug("generate_csr() returning | {} caractère(s)", len(csr))
        return csr

    def replace_certificate(
        self, certificate_uri: str, certificate_string: str, certificate_type: str = "PEM"
    ) -> None:
        """Installe un certificat signé (``CertificateService.ReplaceCertificate``).

        Attention : un certificat invalide ou mal installé peut couper
        l'accès HTTPS au BMC. À tester sur un environnement non critique
        avant un déploiement en production, comme pour tout changement
        réseau/BMC de cette section.
        """
        logger.debug("replace_certificate() called | uri={}", certificate_uri)
        body = {
            "CertificateUri": {"@odata.id": certificate_uri},
            "CertificateString": certificate_string,
            "CertificateType": certificate_type,
        }
        self._request(
            "POST",
            "/redfish/v1/CertificateService/Actions/CertificateService.ReplaceCertificate",
            json=body,
        )
        logger.debug("replace_certificate() returning")

    # ------------------------------------------------------------------
    # Construction d'un ServerProfile (inventaire — pas de dépendances)
    # ------------------------------------------------------------------
    def build_server_profile(self, system_path: str) -> ServerProfile:
        logger.debug("build_server_profile() called | système={}", system_path)
        system = self.get_system(system_path)
        vendor = self._normalize_vendor(system.get("Manufacturer") or "")
        model = (system.get("Model") or "").strip()

        components: dict[str, ComponentState] = {}
        for entry in self.list_firmware_inventory():
            version_raw = entry.get("Version")
            if not version_raw:
                logger.debug("entrée sans version ignorée : {}", entry.get("Id"))
                continue  # entrée sans version exploitable : ignorée, pas devinée
            component_id = entry.get("Id") or ""
            name = entry.get("Name") or ""
            component_type = self._classify(vendor, component_id, name)
            components[component_type] = ComponentState(component_type, FirmwareVersion(version_raw))

        profil = ServerProfile(model=model, vendor=vendor, components=components)
        logger.debug("build_server_profile() returning | {} composant(s)", len(components))
        return profil

    @staticmethod
    def _normalize_vendor(vendor_raw: str) -> str:
        logger.debug("_normalize_vendor() called | constructeur={}", vendor_raw)
        vendor_lower = vendor_raw.lower()
        if "hp" in vendor_lower or "hewlett" in vendor_lower:
            logger.debug("_normalize_vendor() returning | hpe")
            return "hpe"
        if "dell" in vendor_lower:
            logger.debug("_normalize_vendor() returning | dell")
            return "dell"
        # "IBM" couvre les anciens System x, rachetés par Lenovo (même filière
        # de BMC : IMM puis XCC).
        if "lenovo" in vendor_lower or "ibm" in vendor_lower:
            logger.debug("_normalize_vendor() returning | lenovo")
            return "lenovo"
        logger.debug("_normalize_vendor() returning | {}", vendor_lower or "unknown")
        return vendor_lower or "unknown"

    @staticmethod
    def _classify(vendor: str, component_id: str, name: str) -> str:
        logger.debug("_classify() called | constructeur={} identifiant={}", vendor, component_id)
        # Le champ Redfish ``Id`` porte souvent le meilleur indice
        # (ex. "iDRAC.Embedded.1", "RAID.Integrated.1-1") ; on le combine au
        # ``Name`` pour la recherche de mots-clés plutôt que de se fier au
        # seul Name, parfois trop générique ("Integrated Remote Access
        # Controller" ne contient pas littéralement "idrac").
        combined = f"{component_id} {name}".strip()
        if vendor == "dell":
            type_composant = classify_dell_component(component_id, combined)
            logger.debug("_classify() returning | {} (dell)", type_composant)
            return type_composant
        if vendor == "hpe":
            type_composant = classify_hpe_component(combined)
            logger.debug("_classify() returning | {} (hpe)", type_composant)
            return type_composant
        if vendor == "lenovo":
            type_composant = classify_lenovo_component(component_id, combined)
            logger.debug("_classify() returning | {} (lenovo)", type_composant)
            return type_composant
        # Constructeur non reconnu : repli générique — cohérent avec le
        # principe "ne pas deviner" déjà appliqué ailleurs dans le projet.
        logger.debug("_classify() returning | unknown (constructeur non reconnu)")
        return "unknown"
