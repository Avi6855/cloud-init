# This file is part of cloud-init. See LICENSE file for license information.
import logging
import os
from abc import ABC, abstractmethod
from functools import partial
from typing import Callable, Dict, Iterable, List, Optional, Type

from cloudinit import subp, util
from cloudinit.net import eni, netplan, network_manager, networkd
from cloudinit.net.netops.iproute2 import Iproute2
from cloudinit.net.network_state import NetworkState

LOG = logging.getLogger(__name__)

# netplan apply reaches systemd-networkd through networkctl, and networkctl
# needs the system bus. On Ubuntu this stage runs before sysinit.target while
# dbus.socket runs after it, so early in boot the socket is often missing.
SYSTEM_BUS_SOCKET = "/run/dbus/system_bus_socket"

# What netplan prints when it cannot reach that socket. It still exits 0, after
# restarting systemd-networkd, so the exit code alone does not show it.
SYSTEM_BUS_UNAVAILABLE_ERR = "Failed to connect system bus"

# Runtime state kept by the netplan backends. /run is a tmpfs, so these only
# exist once the backend has started in this boot.
NETWORKD_RUNTIME_DIR = "/run/systemd/netif"
NETWORK_MANAGER_RUNTIME_DIR = "/run/NetworkManager"


def system_bus_available() -> bool:
    """Is the system bus socket there?"""
    return os.path.exists(SYSTEM_BUS_SOCKET)


def network_backend_running() -> bool:
    """Has a netplan backend started in this boot?"""
    return os.path.isdir(
        os.path.join(NETWORKD_RUNTIME_DIR, "links")
    ) or os.path.isdir(NETWORK_MANAGER_RUNTIME_DIR)


class NoActivatorException(Exception):
    pass


def _alter_interface(
    cmd: list, device_name: str, warn_on_stderr: bool = True
) -> bool:
    """Attempt to alter an interface using a command list"""
    return _alter_interface_callable(partial(subp.subp, cmd), warn_on_stderr)


def _alter_interface_callable(
    callable: Callable, warn_on_stderr: bool = True
) -> bool:
    """Attempt to alter an interface using a callable

    this function standardizes logging and response to failure for
    various activators
    """
    try:
        _out, err = callable()
        if len(err):
            log_stderr = LOG.warning if warn_on_stderr else LOG.debug
            log_stderr("Received stderr output: %s", err)
        return True
    except subp.ProcessExecutionError as e:
        util.logexc(LOG, "Running interface command %s failed", e.cmd)
        return False


class NetworkActivator(ABC):
    @staticmethod
    @abstractmethod
    def available() -> bool:
        """Return True if activator is available, otherwise return False."""
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    def bring_up_interface(device_name: str) -> bool:
        """Bring up interface.

        Return True is successful, otherwise return False
        """
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    def bring_down_interface(device_name: str) -> bool:
        """Bring down interface.

        Return True is successful, otherwise return False
        """
        raise NotImplementedError()

    @classmethod
    def bring_up_interfaces(cls, device_names: Iterable[str]) -> bool:
        """Bring up specified list of interfaces.

        Return True is successful, otherwise return False
        """
        return all(cls.bring_up_interface(device) for device in device_names)

    @classmethod
    def bring_up_all_interfaces(cls, network_state: NetworkState) -> bool:
        """Bring up all interfaces.

        Return True is successful, otherwise return False
        """
        return cls.bring_up_interfaces(
            [i["name"] for i in network_state.iter_interfaces()]
        )

    @staticmethod
    def wait_for_network() -> None:
        """Wait for network to come up."""
        raise NotImplementedError()


class IfUpDownActivator(NetworkActivator):
    # Note that we're not overriding bring_up_interfaces to pass something
    # like ifup --all because it isn't supported everywhere.
    # E.g., NetworkManager has a ifupdown plugin that requires the name
    # of a specific connection.
    @staticmethod
    def available() -> bool:
        """Return true if ifupdown can be used on this system."""
        return eni.available()

    @staticmethod
    def bring_up_interface(device_name: str) -> bool:
        """Bring up interface using ifup.

        Return True is successful, otherwise return False
        """
        cmd = ["ifup", device_name]
        return _alter_interface(cmd, device_name)

    @staticmethod
    def bring_down_interface(device_name: str) -> bool:
        """Bring up interface using ifup.

        Return True is successful, otherwise return False
        """
        cmd = ["ifdown", device_name]
        return _alter_interface(cmd, device_name)


class IfConfigActivator(NetworkActivator):
    @staticmethod
    def available() -> bool:
        """Return true if ifconfig can be used on this system."""
        expected = "ifconfig"
        search = ["/sbin"]
        return bool(subp.which(expected, search=search))

    @staticmethod
    def bring_up_interface(device_name: str) -> bool:
        """Bring up interface using ifconfig <dev> up.

        Return True is successful, otherwise return False
        """
        cmd = ["ifconfig", device_name, "up"]
        return _alter_interface(cmd, device_name)

    @staticmethod
    def bring_down_interface(device_name: str) -> bool:
        """Bring up interface using ifconfig <dev> down.

        Return True is successful, otherwise return False
        """
        cmd = ["ifconfig", device_name, "down"]
        return _alter_interface(cmd, device_name)


class NetworkManagerActivator(NetworkActivator):
    @staticmethod
    def available() -> bool:
        """Return true if NetworkManager can be used on this system."""
        return network_manager.available()

    @staticmethod
    def bring_up_interface(device_name: str) -> bool:
        """Bring up connection using nmcli.

        Return True is successful, otherwise return False
        """
        from cloudinit.net.network_manager import conn_filename

        filename = conn_filename(device_name)
        if filename is None:
            LOG.warning(
                "Unable to find an interface config file. "
                "Unable to bring up interface."
            )
            return False

        cmd = ["nmcli", "connection", "load", filename]
        if _alter_interface(cmd, device_name):
            cmd = ["nmcli", "connection", "up", "filename", filename]
        else:
            _alter_interface(["nmcli", "connection", "reload"], device_name)
            cmd = ["nmcli", "connection", "up", "ifname", device_name]
        return _alter_interface(cmd, device_name)

    @staticmethod
    def bring_down_interface(device_name: str) -> bool:
        """Bring down interface using nmcli.

        Return True is successful, otherwise return False
        """
        cmd = ["nmcli", "device", "disconnect", device_name]
        return _alter_interface(cmd, device_name)

    @classmethod
    def bring_up_interfaces(cls, device_names: Iterable[str]) -> bool:
        """Activate network

        Return True on success
        """
        state = subp.subp(
            [
                "systemctl",
                "show",
                "--property=SubState",
                "NetworkManager.service",
            ]
        ).stdout.rstrip()
        if "SubState=running" != state:
            LOG.warning(
                "Expected NetworkManager SubState=running, but detected: %s",
                state,
            )
        return _alter_interface(
            ["systemctl", "try-reload-or-restart", "NetworkManager.service"],
            "all",
        ) and all(cls.bring_up_interface(device) for device in device_names)


class NetplanActivator(NetworkActivator):
    NETPLAN_CMD = ["netplan", "apply"]
    NETPLAN_GENERATE_CMD = ["netplan", "generate"]

    @staticmethod
    def _apply() -> bool:
        """Apply the netplan config, avoiding the system bus where we can.

        netplan apply needs the bus to reach networkd. Early in boot it is
        missing, so networkctl fails, netplan restarts networkd while it is
        still coming up and the config ends up half applied. netplan only
        writes a line to stderr, so cloud-init never noticed (GH-4044).

        netplan generate needs no bus and writes what networkd reads at
        startup. networkd starts after this stage, so when the bus is missing
        and networkd is not up yet, generating is enough.
        """
        if not system_bus_available():
            return NetplanActivator._generate_only()
        return NetplanActivator._apply_with_bus()

    @staticmethod
    def _generate_only() -> bool:
        """Write the config for networkd to pick up when it starts."""
        if network_backend_running():
            # The backends do not watch their config directories, so a file
            # written now is only read the next time they start.
            LOG.warning(
                "Cannot apply the netplan configuration: the system bus is "
                "not available and the network backend is already running. "
                "The configuration will be applied the next time that "
                "backend starts."
            )
            NetplanActivator._generate()
            return False
        LOG.info(
            "System bus is not available yet; generating the netplan "
            "configuration for systemd-networkd to apply at startup"
        )
        return NetplanActivator._generate()

    @staticmethod
    def _generate() -> bool:
        return _alter_interface(
            NetplanActivator.NETPLAN_GENERATE_CMD,
            "all",
            warn_on_stderr=False,
        )

    @staticmethod
    def _apply_with_bus() -> bool:
        """Run netplan apply; a bus error on stderr is a failure."""
        try:
            _out, err = subp.subp(NetplanActivator.NETPLAN_CMD)
        except subp.ProcessExecutionError as e:
            util.logexc(LOG, "Running interface command %s failed", e.cmd)
            return False
        if err:
            if SYSTEM_BUS_UNAVAILABLE_ERR in err:
                LOG.warning(
                    "netplan apply could not reach the system bus: %s", err
                )
                return False
            LOG.debug("Received stderr output: %s", err)
        return True

    @staticmethod
    def available() -> bool:
        """Return true if netplan can be used on this system."""
        return netplan.available()

    @staticmethod
    def bring_up_interface(device_name: str) -> bool:
        """Apply netplan config.

        Return True is successful, otherwise return False
        """
        LOG.debug(
            "Calling 'netplan apply' rather than "
            "altering individual interfaces"
        )
        return NetplanActivator._apply()

    @staticmethod
    def bring_up_interfaces(device_names: Iterable[str]) -> bool:
        """Apply netplan config.

        Return True is successful, otherwise return False
        """
        LOG.debug(
            "Calling 'netplan apply' rather than "
            "altering individual interfaces"
        )
        return NetplanActivator._apply()

    @staticmethod
    def bring_up_all_interfaces(network_state: NetworkState) -> bool:
        """Apply netplan config.

        Return True is successful, otherwise return False
        """
        return NetplanActivator._apply()

    @staticmethod
    def bring_down_interface(device_name: str) -> bool:
        """Apply netplan config.

        Return True is successful, otherwise return False
        """
        LOG.debug(
            "Calling 'netplan apply' rather than "
            "altering individual interfaces"
        )
        return NetplanActivator._apply()

    @staticmethod
    def wait_for_network() -> None:
        """On networkd systems, wait for systemd-networkd-wait-online"""
        # At the moment, this is only supported using the networkd renderer.
        if network_manager.available():
            LOG.debug("NetworkManager is enabled, skipping networkd wait")
            return
        NetworkdActivator.wait_for_network()


class NetworkdActivator(NetworkActivator):
    @staticmethod
    def available() -> bool:
        """Return true if ifupdown can be used on this system."""
        return networkd.available()

    @staticmethod
    def bring_up_interface(device_name: str) -> bool:
        """Return True is successful, otherwise return False"""
        return _alter_interface_callable(
            partial(Iproute2.link_up, device_name)
        )

    @staticmethod
    def bring_up_all_interfaces(network_state: NetworkState) -> bool:
        """Return True is successful, otherwise return False"""
        cmd = ["systemctl", "restart", "systemd-networkd", "systemd-resolved"]
        return _alter_interface(cmd, "all")

    @staticmethod
    def bring_down_interface(device_name: str) -> bool:
        """Return True is successful, otherwise return False"""
        return _alter_interface_callable(
            partial(Iproute2.link_down, device_name)
        )

    @staticmethod
    def wait_for_network() -> None:
        """Wait for systemd-networkd-wait-online."""
        subp.subp(
            ["systemctl", "start", "systemd-networkd-wait-online.service"]
        )


# This section is mostly copied and pasted from renderers.py. An abstract
# version to encompass both seems overkill at this point
DEFAULT_PRIORITY = [
    "eni",
    "netplan",
    "network-manager",
    "networkd",
    "ifconfig",
]

NAME_TO_ACTIVATOR: Dict[str, Type[NetworkActivator]] = {
    "eni": IfUpDownActivator,
    "netplan": NetplanActivator,
    "network-manager": NetworkManagerActivator,
    "networkd": NetworkdActivator,
    "ifconfig": IfConfigActivator,
}


def search_activator(priority: List[str]) -> Optional[Type[NetworkActivator]]:
    """Returns the first available activator from the priority list or None."""
    unknown = [i for i in priority if i not in DEFAULT_PRIORITY]
    if unknown:
        raise ValueError(
            f"Unknown activators provided in priority list: {unknown}"
        )
    activator_classes = [NAME_TO_ACTIVATOR[name] for name in priority]
    return next(
        (
            activator_cls
            for activator_cls in activator_classes
            if activator_cls.available()
        ),
        None,
    )


def select_activator(
    priority: Optional[List[str]] = None,
) -> Type[NetworkActivator]:
    if priority is None:
        priority = DEFAULT_PRIORITY
    selected = search_activator(priority)
    if not selected:
        raise NoActivatorException(
            f"No available network activators found. "
            f"Searched through list: {priority}"
        )
    LOG.debug(
        "Using selected activator: %s from priority: %s", selected, priority
    )
    return selected
