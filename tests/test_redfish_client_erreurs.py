"""Chemins d'erreur et de repli de RedfishClient (issue #4).

Complète ``test_redfish_client.py`` : ressources absentes (HTTP 404), champs
manquants, paramètres refusés et branches facultatives des corps de requête.
Même faux transport, aucun BMC réel contacté.

Exécution : python3 tests/test_redfish_client_erreurs.py
"""

from __future__ import annotations

from test_redfish_client import BASE_URL, FakeResponse, FakeTransport

from gcm_redfish_core.redfish_client import RedfishClient, RedfishError

SYSTEME = "/redfish/v1/Systems/1"
CHASSIS = "/redfish/v1/Chassis/1"
MANAGER = "/redfish/v1/Managers/1"


def _client(routes: dict[tuple[str, str], FakeResponse], **kwargs) -> tuple[RedfishClient, FakeTransport]:
    transport = FakeTransport(routes)
    return RedfishClient(BASE_URL, "admin", "secret", session=transport, **kwargs), transport


def _leve(appel, message: str) -> None:
    try:
        appel()
    except RedfishError:
        return
    raise AssertionError(message)


def scenario_ressources_absentes_renvoient_vide() -> None:
    """Chaque collection facultative absente (404) donne une valeur vide."""
    client, _ = _client({})
    assert client.list_firmware_inventory() == []
    assert client.list_event_subscriptions() == []
    assert client.list_volumes(f"{SYSTEME}/Storage/RAID") == []
    assert client.get_thermal(CHASSIS) == {}
    assert client.get_power_readings(CHASSIS) == {}
    assert client.list_log_services(SYSTEME) == []
    assert client.list_log_entries(f"{SYSTEME}/LogServices/Sel") == []
    assert client.list_tasks() == []
    assert client.list_active_sessions() == []
    assert client.list_licenses() == []
    assert client.list_certificates(f"{MANAGER}/NetworkProtocol/HTTPS/Certificates") == []
    print("OK : collections facultatives absentes -> valeurs vides, sans exception.\n")


def scenario_inventaire_entree_illisible_ignoree() -> None:
    routes = {
        ("GET", "/redfish/v1/UpdateService/FirmwareInventory"): FakeResponse(
            200,
            {
                "Members": [
                    {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/BIOS"},
                    {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/Absent"},
                ]
            },
        ),
        ("GET", "/redfish/v1/UpdateService/FirmwareInventory/BIOS"): FakeResponse(
            200, {"Id": "BIOS", "Version": "2.1.0"}
        ),
    }
    client, _ = _client(routes)
    entrees = client.list_firmware_inventory()
    assert [e["Id"] for e in entrees] == ["BIOS"], entrees
    print("OK : une entrée d'inventaire illisible n'empêche pas les autres.\n")


def scenario_session_sans_jeton_repli_basic() -> None:
    """Session créée sans X-Auth-Token : repli sur HTTP Basic."""
    routes = {("POST", "/redfish/v1/SessionService/Sessions"): FakeResponse(201, {}, headers={})}
    client, transport = _client(routes)
    client.connect()
    assert transport.auth == ("admin", "secret")
    print("OK : session sans jeton -> repli HTTP Basic.\n")


def scenario_fermeture_session_en_echec_ignoree() -> None:
    routes = {
        ("POST", "/redfish/v1/SessionService/Sessions"): FakeResponse(
            201, {}, headers={"X-Auth-Token": "tok", "Location": "/redfish/v1/SessionService/Sessions/9"}
        ),
        ("DELETE", "/redfish/v1/SessionService/Sessions/9"): FakeResponse(500, {"error": "boom"}),
    }
    client, transport = _client(routes)
    client.connect()
    client.close()
    assert ("DELETE", "/redfish/v1/SessionService/Sessions/9") in transport.calls
    assert client._auth_token is None and client._session_uri is None
    print("OK : échec de fermeture de session ignoré (best-effort), état remis à zéro.\n")


def scenario_service_root_et_etat_systeme() -> None:
    routes = {
        ("GET", "/redfish/v1/"): FakeResponse(200, {"RedfishVersion": "1.15.0"}),
        ("GET", SYSTEME): FakeResponse(200, {"Status": {"Health": "OK", "State": "Enabled"}}),
    }
    client, _ = _client(routes, verify_ssl=True)
    assert client.get_service_root()["RedfishVersion"] == "1.15.0"
    assert client.get_system_health(SYSTEME) == {"Health": "OK", "State": "Enabled"}
    _leve(lambda: client.get_power_state(SYSTEME), "PowerState absent aurait dû lever RedfishError")
    print("OK : ServiceRoot, santé du système, PowerState absent refusé.\n")


def scenario_actions_alimentation_complementaires() -> None:
    cible = f"{SYSTEME}/Actions/ComputerSystem.Reset"
    routes = {
        ("GET", SYSTEME): FakeResponse(
            200,
            {
                "PowerState": "On",
                "Actions": {
                    "#ComputerSystem.Reset": {
                        "target": cible,
                        "ResetType@Redfish.AllowableValues": ["ForceOff", "GracefulRestart"],
                    }
                },
            },
        ),
        ("POST", cible): FakeResponse(204, {}),
    }
    client, transport = _client(routes)
    client.power_off_force(SYSTEME)
    client.reboot_graceful(SYSTEME)
    assert transport.calls.count(("POST", cible)) == 2
    print("OK : arrêt forcé et redémarrage propre.\n")


def scenario_corps_facultatifs() -> None:
    """Branches où un paramètre facultatif est omis."""
    abonnements = "/redfish/v1/EventService/Subscriptions"
    routes = {
        ("PATCH", f"{MANAGER}/NetworkProtocol"): FakeResponse(204, {}),
        ("POST", abonnements): FakeResponse(201, {}, headers={"Location": f"{abonnements}/1"}),
        ("DELETE", f"{abonnements}/1"): FakeResponse(204, {}),
        ("PATCH", MANAGER): FakeResponse(204, {}),
        ("POST", "/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate"): FakeResponse(
            202, {}, headers={"Location": "/redfish/v1/TaskService/Tasks/7"}
        ),
        ("POST", "/redfish/v1/CertificateService/Actions/CertificateService.GenerateCSR"): FakeResponse(
            200, {"CSRString": "-----BEGIN CERTIFICATE REQUEST-----"}
        ),
    }
    client, transport = _client(routes)
    client.set_snmp_agent(MANAGER, enabled=False)
    assert client.create_snmp_trap_destination("192.0.2.50") == f"{abonnements}/1"
    client.delete_event_subscription(f"{abonnements}/1")
    client.set_datetime(MANAGER, "2026-10-22T20:00:00+02:00")
    assert client.simple_update("http://192.0.2.60/bios.exe") == "/redfish/v1/TaskService/Tasks/7"
    csr = client.generate_csr(f"{MANAGER}/NetworkProtocol/HTTPS/Certificates", "bmc", "Team", "FR")
    assert csr.startswith("-----BEGIN")
    assert ("DELETE", f"{abonnements}/1") in transport.calls
    print("OK : SNMP sans port, trap sans communauté, date sans décalage, mise à jour sans cible, CSR minimale.\n")


def scenario_parametres_refuses() -> None:
    client, transport = _client({})
    _leve(lambda: client.create_syslog_destination("192.0.2.70", protocol="SyslogTCP"), "protocole Syslog inconnu accepté")
    _leve(lambda: client.simple_update("http://192.0.2.60/x", transfer_protocol="GOPHER"), "protocole de transfert accepté")
    _leve(lambda: client.set_ethernet_interface(f"{MANAGER}/EthernetInterfaces/1"), "interface sans changement acceptée")
    assert transport.calls == [], transport.calls
    print("OK : protocoles inconnus et réglage réseau vide refusés sans appel HTTP.\n")


def scenario_bios_sans_objet_settings() -> None:
    routes = {("GET", f"{SYSTEME}/Bios"): FakeResponse(200, {"Attributes": {}})}
    client, transport = _client(routes)
    _leve(lambda: client.set_bios_attributes(SYSTEME, {"BootMode": "Uefi"}), "BIOS sans @Redfish.Settings accepté")
    assert all(methode == "GET" for methode, _ in transport.calls), transport.calls
    print("OK : BIOS sans objet Settings refusé, aucune écriture.\n")


def scenario_journal_sans_limite() -> None:
    service = f"{SYSTEME}/LogServices/Sel"
    entrees = [{"Id": str(i)} for i in range(150)]
    routes = {("GET", f"{service}/Entries"): FakeResponse(200, {"Members": entrees})}
    client, _ = _client(routes)
    assert len(client.list_log_entries(service, limit=None)) == 150
    print("OK : journal complet quand limit=None.\n")


def scenario_profil_constructeurs() -> None:
    """Normalisation et classification pour HPE et un constructeur inconnu."""
    inventaire = "/redfish/v1/UpdateService/FirmwareInventory"
    routes = {
        ("GET", inventaire): FakeResponse(
            200,
            {"Members": [{"@odata.id": f"{inventaire}/1"}, {"@odata.id": f"{inventaire}/2"}]},
        ),
        ("GET", f"{inventaire}/1"): FakeResponse(200, {"Id": "1", "Name": "System ROM", "Version": "U54 v2.10"}),
        ("GET", f"{inventaire}/2"): FakeResponse(200, {"Id": "2", "Name": "Sans version"}),
        ("GET", "/redfish/v1/Systems/hpe"): FakeResponse(200, {"Manufacturer": "HPE", "Model": " ProLiant DL380 "}),
        ("GET", "/redfish/v1/Systems/autre"): FakeResponse(200, {"Manufacturer": "Supermicro", "Model": "X13"}),
        ("GET", "/redfish/v1/Systems/vide"): FakeResponse(200, {}),
    }
    client, _ = _client(routes)
    hpe = client.build_server_profile("/redfish/v1/Systems/hpe")
    assert hpe.vendor == "hpe" and hpe.model == "ProLiant DL380", hpe
    assert len(hpe.components) == 1, hpe.components
    autre = client.build_server_profile("/redfish/v1/Systems/autre")
    assert autre.vendor == "supermicro" and set(autre.components) == {"unknown"}, autre
    assert client.build_server_profile("/redfish/v1/Systems/vide").vendor == "unknown"
    assert RedfishClient._normalize_vendor("IBM") == "lenovo"
    print("OK : profils HPE, constructeur inconnu et fabricant absent.\n")


if __name__ == "__main__":
    scenario_ressources_absentes_renvoient_vide()
    scenario_inventaire_entree_illisible_ignoree()
    scenario_session_sans_jeton_repli_basic()
    scenario_fermeture_session_en_echec_ignoree()
    scenario_service_root_et_etat_systeme()
    scenario_actions_alimentation_complementaires()
    scenario_corps_facultatifs()
    scenario_parametres_refuses()
    scenario_bios_sans_objet_settings()
    scenario_journal_sans_limite()
    scenario_profil_constructeurs()
    print("Tous les scénarios d'erreur ont passé les assertions.")
