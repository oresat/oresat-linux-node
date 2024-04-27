"""OreSat CANopen Node"""

import os
import struct
from enum import IntEnum
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Any, Callable, Dict, Union

import canopen
from loguru import logger

from ..canopen.network import CanNetwork, CanNetworkState
from ..common.daemon import Daemon
from ..common.oresat_file_cache import OreSatFileCache


class NodeStop(IntEnum):
    """Node stop commands."""

    SOFT_RESET = 1
    """Just stop the app and exit. Systemd will restart the app."""
    HARD_RESET = 2
    """Reboot system after app has stopped"""
    FACTORY_RESET = 3
    """Clear all file cachces and reboot system after app has stopped"""
    POWER_OFF = 4
    """Just power off the system."""


class Node:
    """
    OreSat CANopen Node class

    Jobs:
    - It abstracts away the canopen.LocalNode and canopen.Network from Resources and Services.
    - Provides access to the OD for Resources and Services.
    - Lets Resources and Services send TPDOs.
    - Lets Resources and Services send EMCY messages.
    - Set up the file transfer caches.
    - Starts/stops all Resources and Services.
    - Sets up all timer-base TPDOs.
    - Sets up all RPDO callbacks.

    Basically it tries to abstract all the CANopen things as much a possible, while providing a
    basic API for CANopen things.
    """

    def __init__(self, network: CanNetwork, od: canopen.ObjectDictionary):
        """
        Parameters
        ----------
        network: CanNetwork
            The CAN network
        od: canopen.ObjectDictionary
            The CANopen ObjectDictionary
        """

        self._event = Event()
        self._od = od
        self._node: canopen.LocalNode = None
        self._network: CanNetwork = network
        self._read_cbs = {}  # type: ignore
        self._write_cbs = {}  # type: ignore
        self._syncs = 0
        self._reset = NodeStop.SOFT_RESET
        self._daemons = {}  # type: ignore

        if os.geteuid() == 0:  # running as root
            self.work_base_dir = "/var/lib/oresat"
            self.cache_base_dir = "/var/cache/oresat"
        else:
            self.work_base_dir = str(Path.home()) + "/.oresat"
            self.cache_base_dir = str(Path.home()) + "/.cache/oresat"

        fread_path = self.cache_base_dir + "/fread"
        fwrite_path = self.cache_base_dir + "/fwrite"

        self._fread_cache = OreSatFileCache(fread_path)
        self._fwrite_cache = OreSatFileCache(fwrite_path)

        logger.debug(f"fread cache path {self._fread_cache.dir}")
        logger.debug(f"fwrite cache path {self._fwrite_cache.dir}")

        self._start_time = monotonic()
        self._network.monitor()
        self._first_network_reset = True
        self._network.add_reset_callback(self._setup_node)
        self._network.subscribe(0x80, self._on_sync)

        self._rpdo_cobid_to_num: dict[int, int] = {}
        for i in range(self._od.device_information.nr_of_RXPDO):
            cob_id = self._od[0x1400 + i][1].value
            self._rpdo_cobid_to_num[cob_id] = i
            self._network.subscribe(cob_id, self._on_pdo)

    def __del__(self):
        # stop the monitor thread if it is running
        if not self._event.is_set():
            self.stop()

    def _on_sync(self, cob_id: int, data: bytes, timestamp: float):  # pylint: disable=W0613
        """On SYNC message send TPDOs configured to be SYNC-based"""

        self._syncs += 1
        if self._syncs == 241:
            self._syncs = 1

        for i in range(self.od.device_information.nr_of_TXPDO):
            transmission_type = self.od[0x1800 + i][2].value
            if self._syncs % transmission_type == 0:
                self.send_tpdo(i)

    def _on_pdo(self, cob_id: int, data: bytes, timestamp: float):  # pylint: disable=W0613
        rpdo = self._rpdo_cobid_to_num[cob_id]
        maps = self.od[0x1600 + rpdo][0].value

        offset = 0
        for i in range(maps):
            pdo_map = self.od[0x1600 + rpdo][i + 1].value

            if pdo_map == 0:
                break  # nothing todo

            pdo_map_bytes = pdo_map.to_bytes(4, "big")
            index, subindex, size = struct.unpack(">HBB", pdo_map_bytes)
            size //= 8

            # call sdo callback(s) and convert data to bytes
            if isinstance(self.od[index], canopen.objectdictionary.Variable):
                self._node.sdo[index].raw = data[offset : offset + size]
            else:  # record or array
                self._node.sdo[index][subindex].raw = data[offset : offset + size]

            offset += size

    def send_tpdo(self, tpdo: int):
        """
        Send a TPDO. Will not be sent if not node is not in operational state.

        Parameters
        ----------
        tpdo: int
            TPDO number to send, should be between 1 and 16.
        """

        if tpdo < 1:
            raise ValueError("TPDO number must be greather than 1")

        if self._network.status != CanNetworkState.NETWORK_UP:
            logger.debug("network is down cannot send an TPDO message")
            return

        tpdo -= 1  # number to offset

        # PDOs should not be sent if CAN bus not in 'OPERATIONAL' state as defined by spec
        if self._node is None or self._node.nmt.state != "OPERATIONAL":
            return

        cob_id = self.od[0x1800 + tpdo][1].value & 0x3F_FF_FF_FF
        maps = self.od[0x1A00 + tpdo][0].value

        data = b""
        for i in range(maps):
            pdo_map = self.od[0x1A00 + tpdo][i + 1].value

            if pdo_map == 0:
                break  # nothing todo

            pdo_map_bytes = pdo_map.to_bytes(4, "big")
            index, subindex, _ = struct.unpack(">HBB", pdo_map_bytes)

            # call sdo callback(s) and convert data to bytes
            if isinstance(self.od[index], canopen.objectdictionary.Variable):
                value = self._node.sdo[index].phys
                value_bytes = self.od[index].encode_raw(value)
            else:  # record or array
                value = self._node.sdo[index][subindex].phys
                value_bytes = self.od[index][subindex].encode_raw(value)

            # pack pdo with bytes
            data += value_bytes

        self._network.send_message(cob_id, data, False)

    def _on_rpdo_update_od(self, mapping: canopen.pdo.base.Map):
        """Handle parsering an RPDO"""

        for i in mapping.map_array:
            if i == 0:
                continue

            value = mapping.map_array[i].raw
            if value == 0:
                break  # no more mapping

            index, subindex, _ = struct.unpack(">HBB", value.to_bytes(4, "big"))
            if isinstance(self.od[index], canopen.objectdictionary.Variable):
                self._node.sdo[index].raw = mapping.map[i - 1].get_data()
            else:
                self._node.sdo[index][subindex].raw = mapping.map[i - 1].get_data()

    def _setup_node(self):
        """Create the CANopen node."""

        if self._od.node_id is None:
            self._od.node_id = 0x7C

        self._node = canopen.LocalNode(self._od.node_id, self._od)
        self._network.add_node(self._node)
        self._node.nmt.state = "OPERATIONAL"

        self._node.add_read_callback(self._on_sdo_read)
        self._node.add_write_callback(self._on_sdo_write)

        if not self._first_network_reset or monotonic() - self._start_time > 5:
            self.send_emcy(0x8140)
        else:
            self._first_network_reset = False

    def _destroy_node(self):
        """Destroy the CANopen node."""

        self._node = None

    def run(self) -> NodeStop:
        """
        Go into operational mode, start all the resources, start all the threads, and monitor
        everything in a loop.

        Returns
        -------
        NodeStop
            Reset / power off condition.
        """

        logger.info(f"{self.name} node is starting")

        loops = -1
        delay_ms = 100
        delay = delay_ms / 1000
        start_time = monotonic()
        while not self._event.is_set():
            loops += 1
            self._event.wait(delay - ((monotonic() - start_time) % delay))
            self._network.monitor()

            if self._network.status != CanNetworkState.NETWORK_UP:
                continue

            # send heartbeat
            event_time = self.od[0x1017].value
            if loops % (event_time // delay_ms) == 0:
                self._network.send_message(0x700 + self.od.node_id, b"\x05", False)

            # send all timer-based TPDOs
            for i in range(self._od.device_information.nr_of_TXPDO):
                if i + 0x1800 not in self.od:
                    continue
                transmission_type = self.od[0x1800 + i][2].value
                event_time = self.od[0x1800 + i][5].value
                if (
                    transmission_type in [0xFE, 0xFF]
                    and event_time != 0
                    and loops % (event_time // delay_ms) == 0
                ):
                    self.send_tpdo(i + 1)

        self._destroy_node()

        logger.info(f"{self.name} node has ended")
        return self._reset

    def stop(self, reset: Union[NodeStop, None] = None):
        """End the run loop"""

        if reset is not None:
            self._reset = reset
        self._event.set()

    def add_daemon(self, name: str):
        """Add a daemon for the node to monitor and/or control"""

        self._daemons[name] = Daemon(name)

    def add_sdo_callbacks(
        self,
        index: str,
        subindex: str,
        read_cb: Callable[[None], Any],
        write_cb: Callable[[Any], None],
    ):
        """
        Add an SDO read callback for a variable at index and optional subindex.

        Parameters
        ----------
        index: int or str
            The index to call the callback on.
        subindex: int or str
            The subindex to call the callback on.
        read_cb: Callable[[None], Any]
            The SDO read callback. Allows overriding the data being sent on a SDO read. If
            overriding read data return the value or return :py:data:`None` to use the the value
            from the od. Set to :py:data:`None` for no read_cb.
        write_cb: Callable[[Any], None]
            The SDO writecallback. Gives access to the data being received on a SDO write.
            Set to :py:data:`None` for no write_cb.
            **Note:** data is still written to object dictionary before call.
        """

        try:
            self.od[index]
        except KeyError:
            logger.warning(f"index {index} does not exist, ignoring request for new sdo callback")
            return

        if subindex:
            try:
                self.od[index][subindex]
            except KeyError:
                logger.warning(
                    f"subindex {subindex} for index {index} does not exist, ignoring request for "
                    "new sdo callback"
                )
                return

        if read_cb is not None:
            self._read_cbs[index, subindex] = read_cb
        if write_cb is not None:
            self._write_cbs[index, subindex] = write_cb

    def send_emcy(self, code: int, data: bytes = b""):
        """
        Send a EMCY message.

        Parameters
        ----------
        code: int
            The EMCY code.
        data: bytes
            Optional manufacturer error code/data (up to 5 bytes).
        """

        if len(data) > 5:
            raise ValueError("data must be 5 or less bytes")

        frame = code.to_bytes(2, "little") + self.od[0x1001].value.to_bytes(1, "little") + data
        frame += b"\x00" * (5 - len(data))
        self._network.send_message(self.od.node_id + 0x80, frame)
        logger.error(f"sent emcy 0x{code:04X} {data.hex()}")

    def _on_sdo_read(self, index: int, subindex: int, od: canopen.objectdictionary.Variable):
        """
        SDO read callback function. Allows overriding the data being sent on a SDO read. Return
        valid datatype for object, if overriding read data, or :py:data:`None` to use the the value
        on object dictionary.

        Parameters
        ----------
        index: int
            The index the SDO is reading to.
        subindex: int
            The subindex the SDO is reading to.
        od: canopen.objectdictionary.Variable
            The variable object being read to. Badly named. And not appart of the actual OD.

        Returns
        -------
        Any
            The value to return for that index / subindex.
        """

        ret = None

        # convert any ints to strs
        if isinstance(self.od[index], canopen.objectdictionary.Variable) and od == self.od[index]:
            index = od.name
            subindex = None  # type: ignore
        else:
            index = self.od[index].name
            subindex = od.name

        if (index, subindex) in self._read_cbs:
            ret = self._read_cbs[index, subindex]()

        # get value from OD
        if ret is None:
            ret = od.value

        return ret

    def _on_sdo_write(
        self, index: int, subindex: int, od: canopen.objectdictionary.Variable, data: bytes
    ):
        """
        SDO write callback function. Gives access to the data being received on a SDO write.

        *Note:* data is still written to object dictionary before call.

        Parameters
        ----------
        index: int
            The index the SDO being written to.
        subindex: int
            The subindex the SDO being written to.
        od: canopen.objectdictionary.Variable
            The variable object being written to. Badly named.
        data: bytes
            The raw data being written.
        """

        binary_types = [canopen.objectdictionary.DOMAIN, canopen.objectdictionary.OCTET_STRING]

        # set value in OD before callback
        if od.data_type in binary_types:
            od.value = data
        else:
            od.value = od.decode_raw(data)

        # convert any ints to strs
        if isinstance(self.od[index], canopen.objectdictionary.Variable) and od == self.od[index]:
            index = od.name
            subindex = None  # type: ignore
        else:
            index = self.od[index].name
            subindex = od.name

        if (index, subindex) in self._write_cbs:
            self._write_cbs[index, subindex](od.value)

    @property
    def bus(self) -> str:
        """str: The CAN bus."""

        return self._network.channel

    @property
    def bus_state(self) -> str:
        """str: The CAN bus status."""

        return self._network.status.name

    @property
    def name(self) -> str:
        """str: The nodes name."""

        return self._od.device_information.product_name

    @property
    def od(self) -> canopen.ObjectDictionary:
        """canopen.ObjectDictionary: Access to the object dictionary."""

        return self._od

    @property
    def fread_cache(self) -> OreSatFileCache:
        """OreSatFile: Cache the CANopen master node can read to."""

        return self._fread_cache

    @property
    def fwrite_cache(self) -> OreSatFileCache:
        """OreSatFile: Cache the CANopen master node can write to."""

        return self._fwrite_cache

    @property
    def is_running(self) -> bool:
        """bool: Is the node loop running"""

        return not self._event.is_set()

    @property
    def daemons(self) -> Dict[str, Daemon]:
        """dict: The dictionary of external daemons that are monitored and/or controllable"""

        return self._daemons
