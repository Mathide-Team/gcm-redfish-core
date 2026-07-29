"""Valide RedfishClient contre un faux transport HTTP (aucun BMC réel disponible ici).

Le faux transport simule les réponses standard DMTF Redfish d'un serveur
générique. Le schéma reproduit (ServiceRoot, Systems, UpdateService/
FirmwareInventory) est le socle stable et documenté publiquement par le
DMTF — indépendant de tout constructeur.

Exécution : python3 test_redfish_client.py
"""

from __future__ import annotations

import json

import requests

from gcm_redfish_core.redfish_client import RedfishAuthError, RedfishClient, RedfishError

BASE_URL = "https://192.0.2.10"  # adresse de test RFC 5737, non routable


class FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None, headers: dict | None = None):
        self.status_code = status_code
        self._json = json_data or {}
        self.headers = headers or {}
        self.text = json.dumps(self._json)

    def json(self):
        return self._json


class FakeTransport(requests.Session):
    """Faux transport : répond selon une table (méthode, chemin) -> réponse.

    N'hérite de requests.Session que pour satisfaire le typage de
    RedfishClient ; ne fait aucun appel réseau réel.
    """

    def __init__(self, routes: dict[tuple[str, str], FakeResponse]):
        super().__init__()
        self._routes = routes
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, **kwargs):  # noqa: D102 (override requests.Session)
        path = url.replace(BASE_URL, "")
        self.calls.append((method, path))
        key = (method, path)
        if key not in self._routes:
            return FakeResponse(404, {"error": f"route non simulée: {key}"})
        return self._routes[key]


def build_dell_routes() -> dict[tuple[str, str], FakeResponse]:
    return {
        ("POST", "/redfish/v1/SessionService/Sessions"): FakeResponse(
            201,
            {},
            headers={
                "X-Auth-Token": "tok-abc123",
                "Location": "/redfish/v1/SessionService/Sessions/1",
            },
        ),
        ("GET", "/redfish/v1/Systems"): FakeResponse(
            200, {"Members": [{"@odata.id": "/redfish/v1/Systems/System.Embedded.1"}]}
        ),
        ("GET", "/redfish/v1/Managers"): FakeResponse(
            200, {"Members": [{"@odata.id": "/redfish/v1/Managers/iDRAC.Embedded.1"}]}
        ),
        ("GET", "/redfish/v1/Systems/System.Embedded.1"): FakeResponse(
            200,
            {
                "Manufacturer": "Dell Inc.",
                "Model": "PowerEdge R760",
                "PowerState": "On",
                "Actions": {
                    "#ComputerSystem.Reset": {
                        "target": "/redfish/v1/Systems/System.Embedded.1/Actions/ComputerSystem.Reset",
                        "ResetType@Redfish.AllowableValues": [
                            "On",
                            "ForceOff",
                            "GracefulShutdown",
                            "GracefulRestart",
                            "ForceRestart",
                            "Nmi",
                        ],
                    }
                },
            },
        ),
        ("GET", "/redfish/v1/UpdateService/FirmwareInventory"): FakeResponse(
            200,
            {
                "Members": [
                    {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/BIOS"},
                    {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/iDRAC.Embedded.1"},
                    {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/RAID.Integrated.1-1"},
                ]
            },
        ),
        ("GET", "/redfish/v1/UpdateService/FirmwareInventory/BIOS"): FakeResponse(
            200, {"Id": "BIOS", "Name": "BIOS", "Version": "1.6.5"}
        ),
        ("GET", "/redfish/v1/UpdateService/FirmwareInventory/iDRAC.Embedded.1"): FakeResponse(
            200,
            {
                "Id": "iDRAC.Embedded.1",
                "Name": "Integrated Remote Access Controller",
                "Version": "7.10.30.00",
            },
        ),
        ("GET", "/redfish/v1/UpdateService/FirmwareInventory/RAID.Integrated.1-1"): FakeResponse(
            200,
            {"Id": "RAID.Integrated.1-1", "Name": "PERC H755 Controller", "Version": "25.5.9.0001"},
        ),
        ("POST", "/redfish/v1/Systems/System.Embedded.1/Actions/ComputerSystem.Reset"): FakeResponse(
            200, {}
        ),
        ("DELETE", "/redfish/v1/SessionService/Sessions/1"): FakeResponse(200, {}),
    }


def scenario_session_auth_and_profile() -> None:
    print("=== Scénario 1 : session Redfish + construction ServerProfile (Dell) ===")
    transport = FakeTransport(build_dell_routes())
    client = RedfishClient(BASE_URL, "admin", "secret", verify_ssl=False, session=transport)

    with client:
        assert transport.calls[0] == ("POST", "/redfish/v1/SessionService/Sessions")
        system_paths = client.list_system_paths()
        assert system_paths == ["/redfish/v1/Systems/System.Embedded.1"]

        manager_paths = client.list_manager_paths()
        assert manager_paths == ["/redfish/v1/Managers/iDRAC.Embedded.1"]

        profile = client.build_server_profile(system_paths[0])
        print(f"  vendor={profile.vendor} model={profile.model}")
        for comp_type, state in profile.components.items():
            print(f"    {comp_type}: {state.current_version}")

        assert profile.vendor == "dell"
        assert profile.model == "PowerEdge R760"
        assert str(profile.components["bios"].current_version) == "1.6.5"
        assert str(profile.components["bmc"].current_version) == "7.10.30.00"
        assert str(profile.components["raid"].current_version) == "25.5.9.0001"

    assert ("DELETE", "/redfish/v1/SessionService/Sessions/1") in transport.calls
    print("OK : vendor/model/bios/bmc/raid corrects, session fermée en sortie de contexte.\n")


def scenario_session_unavailable_falls_back_to_basic_auth() -> None:
    print("=== Scénario 2 : service de session absent -> repli HTTP Basic Auth ===")
    routes = build_dell_routes()
    del routes[("POST", "/redfish/v1/SessionService/Sessions")]
    routes[("POST", "/redfish/v1/SessionService/Sessions")] = FakeResponse(404, {})

    transport = FakeTransport(routes)
    client = RedfishClient(BASE_URL, "admin", "secret", verify_ssl=False, session=transport)
    client.connect()

    assert transport.auth == ("admin", "secret")
    assert client._auth_token is None
    print("OK : bascule silencieuse sur HTTP Basic Auth, pas d'exception levée.\n")


def scenario_auth_failure_raises() -> None:
    print("=== Scénario 3 : identifiants invalides -> RedfishAuthError ===")
    routes = build_dell_routes()
    routes[("POST", "/redfish/v1/SessionService/Sessions")] = FakeResponse(401, {})
    transport = FakeTransport(routes)
    client = RedfishClient(BASE_URL, "admin", "wrong", verify_ssl=False, session=transport)

    try:
        client._request(
            "POST",
            "/redfish/v1/SessionService/Sessions",
            json={"UserName": "admin", "Password": "wrong"},
        )
        raise AssertionError("RedfishAuthError attendue")
    except RedfishAuthError:
        print("OK : RedfishAuthError bien levée sur un 401.\n")


def build_lenovo_routes() -> dict[tuple[str, str], FakeResponse]:
    return {
        ("POST", "/redfish/v1/SessionService/Sessions"): FakeResponse(
            201,
            {},
            headers={
                "X-Auth-Token": "tok-lenovo",
                "Location": "/redfish/v1/SessionService/Sessions/1",
            },
        ),
        ("GET", "/redfish/v1/Systems"): FakeResponse(
            200, {"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]}
        ),
        ("GET", "/redfish/v1/Systems/1"): FakeResponse(
            200, {"Manufacturer": "Lenovo", "Model": "ThinkSystem SR650 V3"}
        ),
        ("GET", "/redfish/v1/UpdateService/FirmwareInventory"): FakeResponse(
            200,
            {
                "Members": [
                    {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/UEFI"},
                    {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/XCC"},
                    {"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/RAID"},
                ]
            },
        ),
        ("GET", "/redfish/v1/UpdateService/FirmwareInventory/UEFI"): FakeResponse(
            200, {"Id": "UEFI", "Name": "ThinkSystem UEFI", "Version": "PSE142M"}
        ),
        ("GET", "/redfish/v1/UpdateService/FirmwareInventory/XCC"): FakeResponse(
            200, {"Id": "XCC", "Name": "XClarity Controller firmware", "Version": "PSOC02R"}
        ),
        ("GET", "/redfish/v1/UpdateService/FirmwareInventory/RAID"): FakeResponse(
            200, {"Id": "RAID", "Name": "ServeRAID 940-8i firmware", "Version": "51.16.0-4014"}
        ),
        ("DELETE", "/redfish/v1/SessionService/Sessions/1"): FakeResponse(200, {}),
    }


def scenario_lenovo_profile() -> None:
    print("=== Scénario 4 : ServerProfile Lenovo (IBM System x rebrandé -> XCC/IMM) ===")
    transport = FakeTransport(build_lenovo_routes())
    client = RedfishClient(BASE_URL, "USERID", "secret", verify_ssl=False, session=transport)

    with client:
        system_paths = client.list_system_paths()
        profile = client.build_server_profile(system_paths[0])
        print(f"  vendor={profile.vendor} model={profile.model}")
        for comp_type, state in profile.components.items():
            print(f"    {comp_type}: {state.current_version}")

        assert profile.vendor == "lenovo"
        assert profile.components["bios"].current_version.raw == "PSE142M"
        assert profile.components["bmc"].current_version.raw == "PSOC02R"
        assert profile.components["raid"].current_version.raw == "51.16.0-4014"
    print("OK : UEFI->bios, XCC->bmc, ServeRAID->raid correctement classés.\n")


def scenario_power_actions() -> None:
    print("=== Scénario 5 : lecture d'état + reset (power on/off/reboot/NMI) ===")
    transport = FakeTransport(build_dell_routes())
    client = RedfishClient(BASE_URL, "admin", "secret", verify_ssl=False, session=transport)
    system_path = "/redfish/v1/Systems/System.Embedded.1"
    reset_target = f"{system_path}/Actions/ComputerSystem.Reset"

    with client:
        assert client.get_power_state(system_path) == "On"

        allowed = client.allowable_reset_types(system_path)
        assert allowed == {
            "On", "ForceOff", "GracefulShutdown", "GracefulRestart", "ForceRestart", "Nmi",
        }

        client.power_off_graceful(system_path)
        client.power_on(system_path)
        client.reboot_force(system_path)
        client.send_nmi(system_path)

        reset_calls = [c for c in transport.calls if c == ("POST", reset_target)]
        assert len(reset_calls) == 4
    print("OK : power state lu, 4 actions de reset envoyées vers la bonne cible.\n")


def scenario_reset_type_not_allowed_raises() -> None:
    print("=== Scénario 6 : ResetType non annoncé par le BMC -> erreur, pas d'envoi ===")
    transport = FakeTransport(build_dell_routes())
    client = RedfishClient(BASE_URL, "admin", "secret", verify_ssl=False, session=transport)
    system_path = "/redfish/v1/Systems/System.Embedded.1"
    reset_target = f"{system_path}/Actions/ComputerSystem.Reset"

    with client:
        try:
            client.reset_system(system_path, "PushPowerButton")  # absent des AllowableValues simulées
            raise AssertionError("RedfishError attendue")
        except RedfishError as exc:
            assert "PushPowerButton" in str(exc)
        assert ("POST", reset_target) not in transport.calls
    print("OK : aucune action envoyée pour un ResetType non supporté par ce système.\n")


def scenario_snmp_and_syslog() -> None:
    print("=== Scénario 7 : SNMP (agent + trap) et Syslog ===")
    routes = build_dell_routes()
    manager_path = "/redfish/v1/Managers/iDRAC.Embedded.1"
    routes[("GET", f"{manager_path}/NetworkProtocol")] = FakeResponse(
        200, {"SNMP": {"ProtocolEnabled": False, "Port": 161}}
    )
    routes[("PATCH", f"{manager_path}/NetworkProtocol")] = FakeResponse(200, {})
    routes[("GET", "/redfish/v1/EventService/Subscriptions")] = FakeResponse(
        200, {"Members": [{"@odata.id": "/redfish/v1/EventService/Subscriptions/1"}]}
    )
    routes[("POST", "/redfish/v1/EventService/Subscriptions")] = FakeResponse(
        201, {}, headers={"Location": "/redfish/v1/EventService/Subscriptions/2"}
    )
    routes[("GET", "/redfish/v1/EventService/Subscriptions/1")] = FakeResponse(
        200, {"Protocol": "SNMPv2c", "SubscriptionType": "SNMPTrap", "Destination": "snmp://10.0.0.5:162"}
    )
    transport = FakeTransport(routes)
    client = RedfishClient(BASE_URL, "admin", "secret", verify_ssl=False, session=transport)

    with client:
        protocol = client.get_network_protocol(manager_path)
        assert protocol["SNMP"]["ProtocolEnabled"] is False

        client.set_snmp_agent(manager_path, enabled=True, port=161)
        patch_calls = [c for c in transport.calls if c == ("PATCH", f"{manager_path}/NetworkProtocol")]
        assert len(patch_calls) == 1

        trap_location = client.create_snmp_trap_destination(
            "10.0.0.5", community="public", protocol="SNMPv2c"
        )
        assert trap_location == "/redfish/v1/EventService/Subscriptions/2"

        syslog_location = client.create_syslog_destination("10.0.0.9", protocol="SyslogTLS")
        assert syslog_location == "/redfish/v1/EventService/Subscriptions/2"

        subs = client.list_event_subscriptions()
        assert subs[0]["Protocol"] == "SNMPv2c"

        try:
            client.create_snmp_trap_destination("10.0.0.5", protocol="SNMPv4")
            raise AssertionError("RedfishError attendue pour un protocole SNMP invalide")
        except RedfishError:
            pass
    print("OK : agent SNMP activé, trap + syslog créés, protocole invalide refusé.\n")


def scenario_raid_read_only() -> None:
    print("=== Scénario 8 : consultation RAID (lecture seule, pas de création/suppression) ===")
    routes = build_dell_routes()
    system_path = "/redfish/v1/Systems/System.Embedded.1"
    controller_path = f"{system_path}/Storage/RAID.Slot.1-1"
    volume_path = f"{controller_path}/Volumes/Disk.Virtual.0"

    routes[("GET", f"{system_path}/Storage")] = FakeResponse(
        200, {"Members": [{"@odata.id": controller_path}]}
    )
    routes[("GET", controller_path)] = FakeResponse(
        200,
        {
            "Drives": [
                {"@odata.id": f"{controller_path}/Drives/Disk.Bay.0"},
                {"@odata.id": f"{controller_path}/Drives/Disk.Bay.1"},
            ]
        },
    )
    routes[("GET", f"{controller_path}/Volumes")] = FakeResponse(
        200, {"Members": [{"@odata.id": volume_path}]}
    )
    routes[("GET", volume_path)] = FakeResponse(
        200, {"Name": "VD0", "VolumeType": "Mirrored", "CapacityBytes": 1_920_000_000_000}
    )

    transport = FakeTransport(routes)
    client = RedfishClient(BASE_URL, "admin", "secret", verify_ssl=False, session=transport)

    with client:
        controllers = client.list_storage_controllers(system_path)
        assert controllers == [controller_path]

        drives = client.list_drives(controller_path)
        assert len(drives) == 2

        volumes = client.list_volumes(controller_path)
        assert volumes[0]["VolumeType"] == "Mirrored"

        # Aucune méthode d'écriture ne doit exister (retrait volontaire).
        assert not hasattr(client, "create_raid_volume")
        assert not hasattr(client, "delete_raid_volume")
    print("OK : contrôleurs/disques/volumes consultés, aucune API d'écriture RAID présente.\n")


def build_virtual_media_routes() -> dict[tuple[str, str], FakeResponse]:
    manager_path = "/redfish/v1/Managers/iDRAC.Embedded.1"
    media_path = f"{manager_path}/VirtualMedia/CD"
    return {
        ("GET", f"{manager_path}/VirtualMedia"): FakeResponse(
            200, {"Members": [{"@odata.id": media_path}]}
        ),
        ("GET", media_path): FakeResponse(
            200,
            {
                "Id": "CD",
                "Inserted": False,
                "Image": None,
                "Actions": {
                    "#VirtualMedia.InsertMedia": {
                        "target": f"{media_path}/Actions/VirtualMedia.InsertMedia"
                    },
                    "#VirtualMedia.EjectMedia": {
                        "target": f"{media_path}/Actions/VirtualMedia.EjectMedia"
                    },
                },
            },
        ),
        ("POST", f"{media_path}/Actions/VirtualMedia.InsertMedia"): FakeResponse(200, {}),
        ("POST", f"{media_path}/Actions/VirtualMedia.EjectMedia"): FakeResponse(200, {}),
    }


def scenario_virtual_media() -> None:
    print("=== Scénario 9 : montage/éjection d'une ISO (Virtual Media) ===")
    manager_path = "/redfish/v1/Managers/iDRAC.Embedded.1"
    media_path = f"{manager_path}/VirtualMedia/CD"
    transport = FakeTransport(build_virtual_media_routes())
    client = RedfishClient(BASE_URL, "admin", "secret", verify_ssl=False, session=transport)

    with client:
        media_list = client.list_virtual_media(manager_path)
        assert media_list[0]["Inserted"] is False

        client.insert_media(media_path, "http://10.0.0.2/isos/debian-13.iso", write_protected=True)
        insert_calls = [
            c for c in transport.calls
            if c == ("POST", f"{media_path}/Actions/VirtualMedia.InsertMedia")
        ]
        assert len(insert_calls) == 1

        client.eject_media(media_path)
        eject_calls = [
            c for c in transport.calls
            if c == ("POST", f"{media_path}/Actions/VirtualMedia.EjectMedia")
        ]
        assert len(eject_calls) == 1
    print("OK : ISO montée puis éjectée via les actions Redfish standard.\n")


def scenario_health_logs_tasks_boot_console() -> None:
    print("=== Scénario 10 : santé/capteurs, logs, tâches, boot one-shot, console ===")
    routes = build_dell_routes()
    system_path = "/redfish/v1/Systems/System.Embedded.1"
    manager_path = "/redfish/v1/Managers/iDRAC.Embedded.1"
    chassis_path = "/redfish/v1/Chassis/System.Embedded.1"

    # Santé/capteurs
    routes[("GET", "/redfish/v1/Chassis")] = FakeResponse(
        200, {"Members": [{"@odata.id": chassis_path}]}
    )
    routes[("GET", f"{chassis_path}/Thermal")] = FakeResponse(
        200, {"Temperatures": [{"Name": "CPU1", "ReadingCelsius": 42}]}
    )
    routes[("GET", f"{chassis_path}/Power")] = FakeResponse(
        200, {"PowerSupplies": [{"Name": "PSU1", "Status": {"Health": "OK"}}]}
    )

    # Logs (SEL)
    routes[("GET", f"{system_path}/LogServices")] = FakeResponse(
        200, {"Members": [{"@odata.id": f"{system_path}/LogServices/Sel"}]}
    )
    routes[("GET", f"{system_path}/LogServices/Sel/Entries")] = FakeResponse(
        200, {"Members": [{"Id": "1", "Message": "Power supply restored", "Severity": "OK"}]}
    )

    # Tâches
    routes[("GET", "/redfish/v1/TaskService/Tasks")] = FakeResponse(
        200, {"Members": [{"@odata.id": "/redfish/v1/TaskService/Tasks/1"}]}
    )
    routes[("GET", "/redfish/v1/TaskService/Tasks/1")] = FakeResponse(
        200, {"Id": "1", "TaskState": "Running", "PercentComplete": 40}
    )

    # Boot one-shot
    routes[("GET", system_path)] = FakeResponse(
        200,
        {
            **routes[("GET", system_path)]._json,
            "Boot": {
                "BootSourceOverrideTarget": "None",
                "BootSourceOverrideTarget@Redfish.AllowableValues": ["None", "Pxe", "Cd", "UsbExternal"],
            },
        },
    )
    routes[("PATCH", system_path)] = FakeResponse(200, {})

    # Console
    routes[("GET", manager_path)] = FakeResponse(
        200,
        {
            "GraphicalConsole": {"ServiceEnabled": True, "MaxConcurrentSessions": 6},
            "SerialConsole": {"ServiceEnabled": True, "ConnectTypesSupported": ["SSH"]},
        },
    )

    transport = FakeTransport(routes)
    client = RedfishClient(BASE_URL, "admin", "secret", verify_ssl=False, session=transport)

    with client:
        # Santé/capteurs
        chassis_paths = client.list_chassis_paths()
        assert chassis_paths == [chassis_path]
        thermal = client.get_thermal(chassis_path)
        assert thermal["Temperatures"][0]["ReadingCelsius"] == 42
        power = client.get_power_readings(chassis_path)
        assert power["PowerSupplies"][0]["Status"]["Health"] == "OK"

        # Logs
        log_services = client.list_log_services(system_path)
        assert log_services == [f"{system_path}/LogServices/Sel"]
        entries = client.list_log_entries(log_services[0])
        assert entries[0]["Message"] == "Power supply restored"

        # Tâches
        tasks = client.list_tasks()
        assert tasks[0]["TaskState"] == "Running"
        assert client.get_task("/redfish/v1/TaskService/Tasks/1")["PercentComplete"] == 40

        # Boot one-shot
        client.set_boot_override_once(system_path, "Pxe")
        patch_calls = [c for c in transport.calls if c == ("PATCH", system_path)]
        assert len(patch_calls) == 1
        try:
            client.set_boot_override_once(system_path, "Floppy")  # absent des AllowableValues
            raise AssertionError("RedfishError attendue pour une cible de boot non supportée")
        except RedfishError:
            pass
        client.clear_boot_override(system_path)
        assert len([c for c in transport.calls if c == ("PATCH", system_path)]) == 2

        # Console (statut uniquement)
        console_info = client.get_console_info(manager_path)
        assert console_info["GraphicalConsole"]["MaxConcurrentSessions"] == 6
        assert console_info["SerialConsole"]["ConnectTypesSupported"] == ["SSH"]

    print("OK : santé/capteurs, logs, tâches, boot one-shot (avec refus si non supporté), console.\n")


def scenario_led_sessions_datetime_license_update() -> None:
    print("=== Scénario 11 : LED, sessions actives, date/NTP, licence, SimpleUpdate ===")
    routes = build_dell_routes()
    system_path = "/redfish/v1/Systems/System.Embedded.1"
    manager_path = "/redfish/v1/Managers/iDRAC.Embedded.1"

    # LED d'identification
    routes[("GET", system_path)] = FakeResponse(
        200, {**routes[("GET", system_path)]._json, "IndicatorLED": "Off"}
    )
    routes[("PATCH", system_path)] = FakeResponse(200, {})

    # Sessions actives
    routes[("GET", "/redfish/v1/SessionService/Sessions")] = FakeResponse(
        200, {"Members": [{"@odata.id": "/redfish/v1/SessionService/Sessions/1"}]}
    )
    routes[("GET", "/redfish/v1/SessionService/Sessions/1")] = FakeResponse(
        200, {"UserName": "admin", "SessionType": "Redfish"}
    )

    # Date/heure + NTP
    routes[("GET", manager_path)] = FakeResponse(
        200, {"DateTime": "2026-07-28T10:00:00+00:00", "DateTimeLocalOffset": "+00:00"}
    )
    routes[("PATCH", manager_path)] = FakeResponse(200, {})
    routes[("GET", f"{manager_path}/NetworkProtocol")] = FakeResponse(
        200, {"NTP": {"ProtocolEnabled": False, "NTPServers": []}}
    )
    routes[("PATCH", f"{manager_path}/NetworkProtocol")] = FakeResponse(200, {})

    # Licence
    routes[("GET", "/redfish/v1/LicenseService/Licenses")] = FakeResponse(
        200, {"Members": [{"@odata.id": "/redfish/v1/LicenseService/Licenses/1"}]}
    )
    routes[("GET", "/redfish/v1/LicenseService/Licenses/1")] = FakeResponse(
        200, {"Name": "iDRAC9 Enterprise", "Status": {"State": "Enabled"}}
    )

    # SimpleUpdate
    routes[("POST", "/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate")] = FakeResponse(
        202, {}, headers={"Location": "/redfish/v1/TaskService/Tasks/7"}
    )

    transport = FakeTransport(routes)
    client = RedfishClient(BASE_URL, "admin", "secret", verify_ssl=False, session=transport)

    with client:
        # LED
        assert client.get_indicator_led(system_path) == "Off"
        client.set_indicator_led(system_path, "Blinking")
        assert ("PATCH", system_path) in transport.calls
        try:
            client.set_indicator_led(system_path, "Rainbow")
            raise AssertionError("RedfishError attendue pour un état LED invalide")
        except RedfishError:
            pass

        # Sessions
        sessions = client.list_active_sessions()
        assert sessions[0]["UserName"] == "admin"

        # Date/heure + NTP
        dt = client.get_datetime(manager_path)
        assert dt["DateTime"] == "2026-07-28T10:00:00+00:00"
        client.set_datetime(manager_path, "2026-07-28T11:00:00+00:00")
        client.set_ntp_servers(manager_path, ["10.0.0.1", "10.0.0.2"])
        ntp = client.get_ntp_settings(manager_path)
        assert ntp["ProtocolEnabled"] is False  # valeur simulée avant le PATCH (pas relu ici)

        # Licence
        licenses = client.list_licenses()
        assert licenses[0]["Name"] == "iDRAC9 Enterprise"

        # SimpleUpdate
        task_location = client.simple_update(
            "http://10.0.0.5/firmware/bios.exe", transfer_protocol="HTTP"
        )
        assert task_location == "/redfish/v1/TaskService/Tasks/7"
        try:
            client.simple_update("http://10.0.0.5/x.exe", transfer_protocol="CARRIER_PIGEON")
            raise AssertionError("RedfishError attendue pour un protocole de transfert invalide")
        except RedfishError:
            pass

    print("OK : LED, sessions, date/NTP, licence, SimpleUpdate (avec validations) tous corrects.\n")


def scenario_bios_network_certificates() -> None:
    print("=== Scénario 12 : BIOS (via Settings), réseau Manager, certificats TLS ===")
    routes = build_dell_routes()
    system_path = "/redfish/v1/Systems/System.Embedded.1"
    manager_path = "/redfish/v1/Managers/iDRAC.Embedded.1"
    bios_path = f"{system_path}/Bios"
    bios_settings_path = f"{bios_path}/Settings"
    nic_path = f"{manager_path}/EthernetInterfaces/NIC.1"
    cert_collection_path = f"{manager_path}/NetworkProtocol/HTTPS/Certificates"
    cert_path = f"{cert_collection_path}/1"

    # BIOS
    routes[("GET", bios_path)] = FakeResponse(
        200,
        {
            "Attributes": {"BootMode": "Uefi", "NumLock": "On"},
            "@Redfish.Settings": {"SettingsObject": {"@odata.id": bios_settings_path}},
        },
    )
    routes[("GET", bios_settings_path)] = FakeResponse(
        200, {"Attributes": {"BootMode": "Uefi", "NumLock": "Off"}}
    )
    routes[("PATCH", bios_settings_path)] = FakeResponse(200, {})

    # Réseau Manager
    routes[("GET", f"{manager_path}/EthernetInterfaces")] = FakeResponse(
        200, {"Members": [{"@odata.id": nic_path}]}
    )
    routes[("GET", nic_path)] = FakeResponse(
        200, {"DHCPv4": {"DHCPEnabled": True}, "IPv4Addresses": []}
    )
    routes[("PATCH", nic_path)] = FakeResponse(200, {})

    # Certificats TLS
    routes[("GET", cert_collection_path)] = FakeResponse(
        200, {"Members": [{"@odata.id": cert_path}]}
    )
    routes[("GET", cert_path)] = FakeResponse(
        200, {"Issuer": {"CommonName": "Internal CA"}, "ValidNotAfter": "2027-01-01T00:00:00Z"}
    )
    routes[("POST", "/redfish/v1/CertificateService/Actions/CertificateService.GenerateCSR")] = (
        FakeResponse(200, {"CSRString": "-----BEGIN CERTIFICATE REQUEST-----\n...\n"})
    )
    routes[("POST", "/redfish/v1/CertificateService/Actions/CertificateService.ReplaceCertificate")] = (
        FakeResponse(200, {})
    )

    transport = FakeTransport(routes)
    client = RedfishClient(BASE_URL, "admin", "secret", verify_ssl=False, session=transport)

    with client:
        # BIOS
        current = client.get_bios_attributes(system_path)
        assert current["BootMode"] == "Uefi"
        pending = client.get_bios_pending_attributes(system_path)
        assert pending["NumLock"] == "Off"
        client.set_bios_attributes(system_path, {"NumLock": "On"})
        assert ("PATCH", bios_settings_path) in transport.calls
        # Confirme qu'on écrit bien sur Settings, jamais directement sur Bios.
        assert ("PATCH", bios_path) not in transport.calls

        # Réseau Manager
        interfaces = client.list_manager_ethernet_interfaces(manager_path)
        assert interfaces == [nic_path]
        nic = client.get_ethernet_interface(nic_path)
        assert nic["DHCPv4"]["DHCPEnabled"] is True
        client.set_ethernet_interface(
            nic_path, dhcp_enabled=False,
            static_ipv4={"Address": "10.0.0.50", "SubnetMask": "255.255.255.0", "Gateway": "10.0.0.1"},
        )
        assert ("PATCH", nic_path) in transport.calls
        try:
            client.set_ethernet_interface(nic_path)  # rien à changer
            raise AssertionError("RedfishError attendue si aucun champ fourni")
        except RedfishError:
            pass

        # Certificats TLS
        certs = client.list_certificates(cert_collection_path)
        assert certs[0]["Issuer"]["CommonName"] == "Internal CA"
        csr = client.generate_csr(cert_collection_path, "bmc.example.local", "ACME", "FR")
        assert "BEGIN CERTIFICATE REQUEST" in csr
        client.replace_certificate(cert_path, "-----BEGIN CERTIFICATE-----\n...\n")
        assert (
            "POST",
            "/redfish/v1/CertificateService/Actions/CertificateService.ReplaceCertificate",
        ) in transport.calls

    print("OK : BIOS via Settings (jamais en direct), réseau Manager, certificats TLS.\n")


if __name__ == "__main__":
    scenario_session_auth_and_profile()
    scenario_session_unavailable_falls_back_to_basic_auth()
    scenario_auth_failure_raises()
    scenario_lenovo_profile()
    scenario_power_actions()
    scenario_reset_type_not_allowed_raises()
    scenario_snmp_and_syslog()
    scenario_raid_read_only()
    scenario_virtual_media()
    scenario_health_logs_tasks_boot_console()
    scenario_led_sessions_datetime_license_update()
    scenario_bios_network_certificates()
    print("Tous les scénarios ont passé les assertions.")
