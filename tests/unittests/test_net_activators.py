import logging
from collections import namedtuple
from contextlib import ExitStack
from unittest.mock import patch

import pytest
import yaml

from cloudinit import subp
from cloudinit.net.activators import (
    DEFAULT_PRIORITY,
    NAME_TO_ACTIVATOR,
    IfUpDownActivator,
    NetplanActivator,
    NetworkdActivator,
    NetworkManagerActivator,
    NoActivatorException,
    search_activator,
    select_activator,
)
from cloudinit.net.network_state import parse_net_config_data

V1_CONFIG = """\
version: 1
config:
- type: physical
  name: eth0
- type: physical
  name: eth1
"""

V2_CONFIG = """\
version: 2
ethernets:
  eth0:
    dhcp4: true
  eth1:
    dhcp4: true
"""

NETPLAN_CALL_LIST: list = [
    ((["netplan", "apply"],), {}),
]


@pytest.fixture
def available_mocks():
    mocks = namedtuple("Mocks", "m_which, m_file, m_exists")
    with ExitStack() as mocks_context:
        mocks_context.enter_context(
            patch("cloudinit.distros.uses_systemd", return_value=False)
        )
        m_which = mocks_context.enter_context(
            patch("cloudinit.subp.which", return_value=True)
        )
        m_file = mocks_context.enter_context(
            patch("os.path.isfile", return_value=True)
        )
        m_exists = mocks_context.enter_context(
            patch("os.path.exists", return_value=True)
        )
        yield mocks(m_which, m_file, m_exists)


@pytest.fixture
def unavailable_mocks():
    mocks = namedtuple("Mocks", "m_which, m_file, m_exists")
    with ExitStack() as mocks_context:
        mocks_context.enter_context(
            patch("cloudinit.distros.uses_systemd", return_value=False)
        )
        m_which = mocks_context.enter_context(
            patch("cloudinit.subp.which", return_value=False)
        )
        m_file = mocks_context.enter_context(
            patch("os.path.isfile", return_value=False)
        )
        m_exists = mocks_context.enter_context(
            patch("os.path.exists", return_value=False)
        )
        yield mocks(m_which, m_file, m_exists)


class TestSearchAndSelect:
    def test_empty_list(self, available_mocks):
        resp = search_activator(priority=DEFAULT_PRIORITY)
        assert resp == NAME_TO_ACTIVATOR[DEFAULT_PRIORITY[0]]

        activator = select_activator()
        assert activator == NAME_TO_ACTIVATOR[DEFAULT_PRIORITY[0]]

    def test_priority(self, available_mocks):
        new_order = ["netplan", "network-manager"]
        resp = search_activator(priority=new_order)
        assert resp == NetplanActivator

        activator = select_activator(priority=new_order)
        assert activator == NetplanActivator

    @patch(
        "cloudinit.net.activators.IfUpDownActivator.available",
        return_value=False,
    )
    def test_first_not_available(self, m_available, available_mocks):
        # We've mocked out IfUpDownActivator as unavailable, so expect the
        # next in the list of default priorities
        resp = search_activator(priority=DEFAULT_PRIORITY)
        assert resp == NAME_TO_ACTIVATOR[DEFAULT_PRIORITY[1]]

        resp = select_activator()
        assert resp == NAME_TO_ACTIVATOR[DEFAULT_PRIORITY[1]]

    def test_priority_not_exist(self, available_mocks):
        with pytest.raises(ValueError):
            search_activator(priority=["spam", "eggs"])
        with pytest.raises(ValueError):
            select_activator(priority=["spam", "eggs"])

    def test_none_available(self, unavailable_mocks):
        resp = search_activator(priority=DEFAULT_PRIORITY)
        assert resp is None

        with pytest.raises(NoActivatorException):
            select_activator()


IF_UP_DOWN_AVAILABLE_CALLS = [
    (("ifquery",), {"search": ["/sbin", "/usr/sbin"]}),
    (("ifup",), {"search": ["/sbin", "/usr/sbin"]}),
    (("ifdown",), {"search": ["/sbin", "/usr/sbin"]}),
]

NETPLAN_AVAILABLE_CALLS = [
    (("netplan",), {"search": ["/usr/sbin", "/sbin"]}),
]

NETWORKD_AVAILABLE_CALLS = [
    (("ip",), {"search": ["/usr/sbin", "/bin"]}),
    (("systemctl",), {"search": ["/usr/sbin", "/bin"]}),
]


@pytest.mark.parametrize(
    "activator, available_calls",
    [
        (IfUpDownActivator, IF_UP_DOWN_AVAILABLE_CALLS),
        (NetplanActivator, NETPLAN_AVAILABLE_CALLS),
        (NetworkdActivator, NETWORKD_AVAILABLE_CALLS),
    ],
)
class TestActivatorsAvailable:
    def test_available(self, activator, available_calls, available_mocks):
        activator.available()
        assert available_mocks.m_which.call_args_list == available_calls


IF_UP_DOWN_BRING_UP_CALL_LIST: list = [
    ((["ifup", "eth0"],), {}),
    ((["ifup", "eth1"],), {}),
]

NETWORK_MANAGER_BRING_UP_CALL_LIST: list = [
    (
        (
            [
                "nmcli",
                "connection",
                "load",
                "".join(
                    [
                        "/etc/NetworkManager/system-connections",
                        "/cloud-init-eth0.nmconnection",
                    ]
                ),
            ],
        ),
        {},
    ),
    (
        (
            [
                "nmcli",
                "connection",
                "up",
                "filename",
                "".join(
                    [
                        "/etc/NetworkManager/system-connections",
                        "/cloud-init-eth0.nmconnection",
                    ]
                ),
            ],
        ),
        {},
    ),
    (
        (
            [
                "nmcli",
                "connection",
                "load",
                "".join(
                    [
                        "/etc/NetworkManager/system-connections",
                        "/cloud-init-eth1.nmconnection",
                    ]
                ),
            ],
        ),
        {},
    ),
    (
        (
            [
                "nmcli",
                "connection",
                "up",
                "filename",
                "".join(
                    [
                        "/etc/NetworkManager/system-connections",
                        "/cloud-init-eth1.nmconnection",
                    ]
                ),
            ],
        ),
        {},
    ),
]

NETWORK_MANAGER_BRING_UP_ALL_CALL_LIST: list = [
    (
        (
            [
                "systemctl",
                "show",
                "--property=SubState",
                "NetworkManager.service",
            ],
        ),
        {},
    ),
    ((["systemctl", "try-reload-or-restart", "NetworkManager.service"],), {}),
] + NETWORK_MANAGER_BRING_UP_CALL_LIST

NETWORKD_BRING_UP_CALL_LIST: list = [
    ((["ip", "link", "set", "dev", "eth0", "up"],), {}),
    ((["ip", "link", "set", "dev", "eth1", "up"],), {}),
    ((["systemctl", "restart", "systemd-networkd", "systemd-resolved"],), {}),
]


@pytest.mark.parametrize(
    "activator, expected_call_list",
    [
        (IfUpDownActivator, IF_UP_DOWN_BRING_UP_CALL_LIST),
        (NetplanActivator, NETPLAN_CALL_LIST),
        (NetworkManagerActivator, NETWORK_MANAGER_BRING_UP_CALL_LIST),
        (NetworkdActivator, NETWORKD_BRING_UP_CALL_LIST),
    ],
)
class TestActivatorsBringUp:
    @patch("cloudinit.subp.subp", return_value=("", "Some warning condition"))
    def test_bring_up_interface_log_level_on_stderr(
        self, m_subp, activator, expected_call_list, available_mocks, caplog
    ):
        """Activator stderr logged debug for netplan and warning for others."""
        if activator == NetplanActivator:
            log_level = logging.DEBUG
        else:
            log_level = logging.WARNING
        with caplog.at_level(log_level):
            activator.bring_up_interface("eth0")
        index = 0
        for call in m_subp.call_args_list:
            assert call == expected_call_list[index]
            index += 1
        assert "Received stderr output: Some warning condition" in caplog.text

    @patch("cloudinit.subp.subp", return_value=("", ""))
    def test_bring_up_interface(
        self, m_subp, activator, expected_call_list, available_mocks
    ):
        index = 0
        activator.bring_up_interface("eth0")
        for call in m_subp.call_args_list:
            assert call == expected_call_list[index]
            index += 1


@pytest.mark.parametrize(
    "activator, expected_call_list",
    [
        (IfUpDownActivator, IF_UP_DOWN_BRING_UP_CALL_LIST),
        (NetplanActivator, NETPLAN_CALL_LIST),
        (NetworkManagerActivator, NETWORK_MANAGER_BRING_UP_ALL_CALL_LIST),
        (NetworkdActivator, NETWORKD_BRING_UP_CALL_LIST),
    ],
)
class TestActivatorsBringUpAll:
    @patch("cloudinit.subp.subp", return_value=subp.SubpResult("", ""))
    def test_bring_up_interfaces(
        self, m_subp, activator, expected_call_list, available_mocks
    ):
        index = 0
        activator.bring_up_interfaces(["eth0", "eth1"])
        for call in m_subp.call_args_list:
            assert call == expected_call_list[index]
            index += 1

    @patch("cloudinit.subp.subp", return_value=subp.SubpResult("", ""))
    def test_bring_up_all_interfaces_v1(
        self, m_subp, activator, expected_call_list, available_mocks
    ):
        network_state = parse_net_config_data(yaml.safe_load(V1_CONFIG))
        activator.bring_up_all_interfaces(network_state)
        for call in m_subp.call_args_list:
            assert call in expected_call_list

    @patch("cloudinit.subp.subp", return_value=subp.SubpResult("", ""))
    def test_bring_up_all_interfaces_v2(
        self, m_subp, activator, expected_call_list, available_mocks
    ):
        network_state = parse_net_config_data(yaml.safe_load(V2_CONFIG))
        activator.bring_up_all_interfaces(network_state)
        for call in m_subp.call_args_list:
            assert call in expected_call_list


IF_UP_DOWN_BRING_DOWN_CALL_LIST: list = [
    ((["ifdown", "eth0"],), {}),
    ((["ifdown", "eth1"],), {}),
]

NETWORK_MANAGER_BRING_DOWN_CALL_LIST: list = [
    ((["nmcli", "device", "disconnect", "eth0"],), {}),
    ((["nmcli", "device", "disconnect", "eth1"],), {}),
]

NETWORKD_BRING_DOWN_CALL_LIST: list = [
    ((["ip", "link", "set", "dev", "eth0", "down"],), {}),
    ((["ip", "link", "set", "dev", "eth1", "down"],), {}),
]


@pytest.mark.parametrize(
    "activator, expected_call_list",
    [
        (IfUpDownActivator, IF_UP_DOWN_BRING_DOWN_CALL_LIST),
        (NetplanActivator, NETPLAN_CALL_LIST),
        (NetworkManagerActivator, NETWORK_MANAGER_BRING_DOWN_CALL_LIST),
        (NetworkdActivator, NETWORKD_BRING_DOWN_CALL_LIST),
    ],
)
class TestActivatorsBringDown:
    @patch("cloudinit.subp.subp", return_value=("", ""))
    def test_bring_down_interface(
        self, m_subp, activator, expected_call_list, available_mocks
    ):
        activator.bring_down_interface("eth0")
        assert len(m_subp.call_args_list) == 1
        assert m_subp.call_args_list[0] == expected_call_list[0]


class TestNetworkManagerActivatorBringUp:
    @patch("cloudinit.subp.subp", return_value=("", ""))
    @patch(
        "cloudinit.net.network_manager.available_nm_ifcfg_rh",
        return_value=True,
    )
    @patch("os.path.isfile")
    @patch("os.path.exists", return_value=True)
    def test_bring_up_interface_no_nm_conn(
        self, m_exists, m_isfile, m_plugin, m_subp
    ):
        """
        There is no network manager connection file but ifcfg-rh plugin is
        present and ifcfg interface config files are also present. In this
        case, we should use ifcfg files.
        """

        def fake_isfile_no_nmconn(filename):
            return False if filename.endswith(".nmconnection") else True

        m_isfile.side_effect = fake_isfile_no_nmconn

        expected_call_list = [
            (
                (
                    [
                        "nmcli",
                        "connection",
                        "load",
                        "".join(
                            [
                                "/etc/sysconfig/network-scripts/ifcfg-eth0",
                            ]
                        ),
                    ],
                ),
                {},
            ),
            (
                (
                    [
                        "nmcli",
                        "connection",
                        "up",
                        "filename",
                        "".join(
                            [
                                "/etc/sysconfig/network-scripts/ifcfg-eth0",
                            ]
                        ),
                    ],
                ),
                {},
            ),
        ]

        index = 0
        assert NetworkManagerActivator.bring_up_interface("eth0")
        for call in m_subp.call_args_list:
            assert call == expected_call_list[index]
            index += 1

    @patch("cloudinit.subp.subp", return_value=("", ""))
    @patch(
        "cloudinit.net.network_manager.available_nm_ifcfg_rh",
        return_value=False,
    )
    @patch("os.path.isfile")
    @patch("os.path.exists", return_value=True)
    def test_bring_up_interface_no_plugin_no_nm_conn(
        self, m_exists, m_isfile, m_plugin, m_subp
    ):
        """
        The ifcfg-rh plugin is absent and nmconnection file is also
        not present. In this case, we can't use ifcfg file and the
        interface bring up should fail.
        """

        def fake_isfile_no_nmconn(filename):
            return False if filename.endswith(".nmconnection") else True

        m_isfile.side_effect = fake_isfile_no_nmconn
        assert not NetworkManagerActivator.bring_up_interface("eth0")

    @patch("cloudinit.subp.subp", return_value=("", ""))
    @patch(
        "cloudinit.net.network_manager.available_nm_ifcfg_rh",
        return_value=True,
    )
    @patch("os.path.isfile", return_value=False)
    @patch("os.path.exists", return_value=True)
    def test_bring_up_interface_no_conn_file(
        self, m_exists, m_isfile, m_plugin, m_subp
    ):
        """
        Neither network manager connection files are present nor
        ifcfg files are present. Even if ifcfg-rh plugin is present,
        we can not bring up the interface. So bring_up_interface()
        should fail.
        """
        assert not NetworkManagerActivator.bring_up_interface("eth0")


class TestNetplanDoesNotRequireSystemBus:
    """GH-4044: `netplan apply` must not need the D-Bus system bus.

    The network stage is ordered before sysinit.target on Ubuntu while
    dbus.socket is ordered after it, so the bus does not exist yet. netplan
    then cannot reach systemd-networkd, restarts it mid-startup and leaves the
    configuration half applied, reporting the problem only on stderr.
    """

    @patch(
        "cloudinit.net.activators.network_backend_running", return_value=False
    )
    @patch("cloudinit.net.activators.system_bus_available", return_value=False)
    @patch("cloudinit.subp.subp", return_value=subp.SubpResult("", ""))
    def test_generates_when_bus_missing_and_backend_not_started(
        self, m_subp, _m_bus, _m_backend
    ):
        """The generated config is applied by the backend when it starts."""
        assert NetplanActivator.bring_up_all_interfaces(None) is True
        assert [c.args[0] for c in m_subp.call_args_list] == [
            ["netplan", "generate"]
        ]

    @patch(
        "cloudinit.net.activators.network_backend_running", return_value=False
    )
    @patch("cloudinit.net.activators.system_bus_available", return_value=False)
    @patch("cloudinit.subp.subp", return_value=subp.SubpResult("", ""))
    def test_never_runs_apply_without_the_bus(
        self, m_subp, _m_bus, _m_backend
    ):
        """Every activation entrypoint avoids the racy apply."""
        assert NetplanActivator.bring_up_interface("eth0") is True
        assert NetplanActivator.bring_up_interfaces(["eth0"]) is True
        assert NetplanActivator.bring_up_all_interfaces(None) is True
        assert NetplanActivator.bring_down_interface("eth0") is True
        assert ["netplan", "apply"] not in [
            call.args[0] for call in m_subp.call_args_list
        ]

    @patch(
        "cloudinit.net.activators.network_backend_running", return_value=True
    )
    @patch("cloudinit.net.activators.system_bus_available", return_value=False)
    @patch("cloudinit.subp.subp", return_value=subp.SubpResult("", ""))
    def test_backend_already_running_is_a_reported_failure(
        self, m_subp, _m_bus, _m_backend
    ):
        """Nothing can apply the config, so don't claim success."""
        assert NetplanActivator.bring_up_all_interfaces(None) is False
        # Still generated, so the next networkd start picks it up.
        assert [c.args[0] for c in m_subp.call_args_list] == [
            ["netplan", "generate"]
        ]

    @patch("cloudinit.net.activators.system_bus_available", return_value=True)
    @patch("cloudinit.subp.subp", return_value=subp.SubpResult("", ""))
    def test_apply_is_used_when_the_bus_exists(self, m_subp, _m_bus):
        """Normal boots keep the fast path."""
        assert NetplanActivator.bring_up_all_interfaces(None) is True
        assert [c.args[0] for c in m_subp.call_args_list] == [
            ["netplan", "apply"]
        ]

    @patch("cloudinit.net.activators.system_bus_available", return_value=True)
    @patch(
        "cloudinit.subp.subp",
        return_value=subp.SubpResult(
            "", "Failed to connect system bus: No such file or directory"
        ),
    )
    def test_missing_bus_error_is_not_silent_success(self, m_subp, _m_bus):
        """The reported symptom must be visible to the caller."""
        assert NetplanActivator.bring_up_all_interfaces(None) is False

    @patch("cloudinit.net.activators.system_bus_available", return_value=True)
    @patch("cloudinit.subp.subp", return_value=subp.SubpResult("", "harmless"))
    def test_unrelated_stderr_is_still_success(self, m_subp, _m_bus):
        """Unchanged behaviour for every other stderr message."""
        assert NetplanActivator.bring_up_all_interfaces(None) is True
