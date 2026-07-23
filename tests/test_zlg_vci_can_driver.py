import ctypes
import unittest

from d7_pmu_iap_tool.can.can_frame import CanFrame
from d7_pmu_iap_tool.can.zlg_vci_can_driver import (
    STATUS_OK,
    TYPE_CAN,
    ZCAN_USBCAN1,
    ZCAN_USBCAN2,
    ZcanChannelInitConfig,
    ZcanReceiveData,
    ZcanReceiveFdData,
    ZcanTransmitData,
    ZcanTransmitFdData,
    ZlgVciCanDriver,
    frame_to_zcan_transmit_data,
    frame_to_zcan_transmit_fd_data,
    timing_for_baudrate,
    zcan_receive_data_to_frame,
    zcan_receive_fd_data_to_frame,
)

# Fake handle values returned by the stubbed DLL (any non-zero value works).
FAKE_DEVICE_HANDLE = 0x1000
FAKE_CHANNEL_HANDLE = 0x2000


class FakeFunction:
    def __init__(self, func):
        self.func = func
        self.calls = []
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        return self.func(*args)


class FakeZcanDll:
    def __init__(self, open_results=None):
        # Map device_type -> handle (0 means open fails for that type).
        self.open_results = open_results or {}
        self.ZCAN_OpenDevice = FakeFunction(self._open_device)
        self.ZCAN_CloseDevice = FakeFunction(lambda *_: STATUS_OK)
        self.ZCAN_InitCAN = FakeFunction(lambda *_: FAKE_CHANNEL_HANDLE)
        self.ZCAN_StartCAN = FakeFunction(lambda *_: STATUS_OK)
        self.ZCAN_ResetCAN = FakeFunction(lambda *_: STATUS_OK)
        self.ZCAN_Transmit = FakeFunction(lambda *_: 1)
        self.ZCAN_Receive = FakeFunction(self._receive)

    def _open_device(self, device_type, *_):
        return self.open_results.get(device_type, FAKE_DEVICE_HANDLE)

    def _receive(self, _channel_handle, arr_ptr, _length, _wait_time):
        arr = ctypes.cast(arr_ptr, ctypes.POINTER(ZcanReceiveData))
        arr[0].frame.can_id = 0x7FF
        arr[0].frame.can_dlc = 3
        arr[0].frame.data[0] = 0x16
        arr[0].frame.data[1] = 0x19
        arr[0].frame.data[2] = 0x02
        return 1


class FakeZcanFdDll(FakeZcanDll):
    def __init__(self):
        super().__init__()
        self.ZCAN_SetValue = FakeFunction(lambda *_: STATUS_OK)
        self.ZCAN_TransmitFD = FakeFunction(lambda *_: 1)
        self.ZCAN_ReceiveFD = FakeFunction(self._receive_fd)

    def _receive_fd(self, _channel_handle, arr_ptr, _length, _wait_time):
        arr = ctypes.cast(arr_ptr, ctypes.POINTER(ZcanReceiveFdData))
        arr[0].frame.can_id = 0x101
        arr[0].frame.len = 12
        arr[0].frame.brs = 1
        for index, byte in enumerate(bytes.fromhex("40 00 40 28 1C 01 06 01 00 41 F0 7A")):
            arr[0].frame.data[index] = byte
        return 1


class TimingForBaudrateTests(unittest.TestCase):
    def test_known_baud_rates(self):
        self.assertEqual(timing_for_baudrate(1_000_000), (0x00, 0x14))
        self.assertEqual(timing_for_baudrate(500_000), (0x00, 0x1C))
        self.assertEqual(timing_for_baudrate(250_000), (0x01, 0x1C))

    def test_unsupported_baud_raises(self):
        with self.assertRaises(ValueError):
            timing_for_baudrate(123)


class ZlgVciCanDriverTests(unittest.TestCase):
    def test_default_device_type_is_usbcan1(self):
        self.assertEqual(ZCAN_USBCAN1, 3)
        self.assertEqual(ZCAN_USBCAN2, 4)
        self.assertEqual(ZlgVciCanDriver().device_type, ZCAN_USBCAN1)

    def test_opens_device_with_zcan_handle_api_and_can_timing(self):
        dll = FakeZcanDll()
        driver = ZlgVciCanDriver()
        driver._dll = dll

        opened = driver.open(ZCAN_USBCAN1, 0, 1, 500_000)

        self.assertTrue(opened)
        self.assertTrue(driver.is_open())
        # Open with the requested device type/index.
        self.assertEqual(dll.ZCAN_OpenDevice.calls[0], (3, 0, 0))
        # InitCAN uses the device handle and channel index, classic CAN + timing.
        self.assertEqual(dll.ZCAN_InitCAN.calls[0][0], FAKE_DEVICE_HANDLE)
        self.assertEqual(dll.ZCAN_InitCAN.calls[0][1], 1)
        config = dll.ZCAN_InitCAN.calls[0][2]._obj
        self.assertEqual(config.can_type, TYPE_CAN)
        self.assertEqual(config.config.can.acc_code, 0x00000000)
        self.assertEqual(config.config.can.acc_mask, 0xFFFFFFFF)
        self.assertEqual(config.config.can.filter, 0)
        self.assertEqual(config.config.can.timing0, 0x00)
        self.assertEqual(config.config.can.timing1, 0x1C)  # 500k
        self.assertEqual(config.config.can.mode, 0)
        # StartCAN uses the channel handle returned by InitCAN.
        self.assertEqual(dll.ZCAN_StartCAN.calls[0], (FAKE_CHANNEL_HANDLE,))

    def test_rejects_unsupported_baudrate_before_opening(self):
        dll = FakeZcanDll()
        driver = ZlgVciCanDriver()
        driver._dll = dll

        opened = driver.open(ZCAN_USBCAN1, 0, 0, 123)

        self.assertFalse(opened)
        self.assertFalse(driver.is_open())
        self.assertEqual(dll.ZCAN_OpenDevice.calls, [])
        self.assertIn("unsupported baudrate", driver.last_error)

    def test_falls_back_to_next_device_type_when_open_fails(self):
        dll = FakeZcanDll(open_results={ZCAN_USBCAN1: 0})
        driver = ZlgVciCanDriver()
        driver._dll = dll

        opened = driver.open(ZCAN_USBCAN1, 0, 0, 500_000)

        self.assertTrue(opened)
        # First the requested (failing) type, then the next candidate.
        self.assertEqual(dll.ZCAN_OpenDevice.calls[0], (3, 0, 0))
        self.assertEqual(dll.ZCAN_OpenDevice.calls[1][0], ZCAN_USBCAN2)
        self.assertEqual(driver.device_type, ZCAN_USBCAN2)

    def test_open_reports_error_when_all_types_fail(self):
        dll = FakeZcanDll(open_results={3: 0, 4: 0})
        driver = ZlgVciCanDriver()
        driver._dll = dll

        opened = driver.open(ZCAN_USBCAN1, 0, 0, 500_000)

        self.assertFalse(opened)
        self.assertFalse(driver.is_open())
        self.assertIn("ZCAN_OpenDevice failed", driver.last_error)

    def test_close_resets_channel_and_closes_device_by_handle(self):
        dll = FakeZcanDll()
        driver = ZlgVciCanDriver()
        driver._dll = dll
        self.assertTrue(driver.open(ZCAN_USBCAN1, 0, 0, 500_000))

        driver.close()

        self.assertFalse(driver.is_open())
        self.assertEqual(dll.ZCAN_ResetCAN.calls[-1], (FAKE_CHANNEL_HANDLE,))
        self.assertEqual(dll.ZCAN_CloseDevice.calls[-1], (FAKE_DEVICE_HANDLE,))

    def test_send_uses_channel_handle(self):
        dll = FakeZcanDll()
        driver = ZlgVciCanDriver()
        driver._dll = dll
        driver.open(ZCAN_USBCAN1, 0, 0, 500_000)

        sent = driver.send(CanFrame(id=0x7FF, data=b"\x16\x19\x02"))

        self.assertTrue(sent)
        self.assertEqual(dll.ZCAN_Transmit.calls[-1][0], FAKE_CHANNEL_HANDLE)
        self.assertEqual(dll.ZCAN_Transmit.calls[-1][2], 1)
        data = ctypes.cast(dll.ZCAN_Transmit.calls[-1][1], ctypes.POINTER(ZcanTransmitData))
        self.assertEqual(data[0].frame.can_id, 0x7FF)
        self.assertEqual(bytes(data[0].frame.data[:3]), b"\x16\x19\x02")

    def test_receive_uses_channel_handle_and_wait_time(self):
        dll = FakeZcanDll()
        driver = ZlgVciCanDriver()
        driver._dll = dll
        driver.open(ZCAN_USBCAN1, 0, 1, 500_000)

        frame = driver.receive(timeout_ms=50)

        self.assertIsNotNone(frame)
        self.assertEqual(dll.ZCAN_Receive.calls[-1][0], FAKE_CHANNEL_HANDLE)
        self.assertEqual(dll.ZCAN_Receive.calls[-1][2], 1)
        self.assertEqual(dll.ZCAN_Receive.calls[-1][3], 50)
        self.assertEqual(frame.id, 0x7FF)
        self.assertEqual(frame.data[:3], b"\x16\x19\x02")

    def test_maps_standard_data_frame_to_zcan_transmit_data(self):
        frame = CanFrame(id=0x7FF, data=b"\x16\x19\x02")

        data = frame_to_zcan_transmit_data(frame)

        self.assertEqual(data.transmit_type, 0)
        self.assertEqual(data.frame.can_id, 0x7FF)
        self.assertEqual(data.frame.can_dlc, 3)
        self.assertEqual(data.frame.eff, 0)
        self.assertEqual(data.frame.rtr, 0)
        self.assertEqual(bytes(data.frame.data[:3]), b"\x16\x19\x02")

    def test_maps_zcan_receive_data_to_can_frame(self):
        data = ZcanReceiveData()
        data.frame.can_id = 0x123
        data.frame.can_dlc = 3
        data.frame.eff = 1
        data.frame.rtr = 0
        data.frame.data[0] = 1
        data.frame.data[1] = 2
        data.frame.data[2] = 3

        frame = zcan_receive_data_to_frame(data)

        self.assertEqual(frame.id, 0x123)
        self.assertEqual(frame.dlc, 3)
        self.assertEqual(frame.data[:3], b"\x01\x02\x03")
        self.assertTrue(frame.extended)

    def test_init_config_is_a_ctypes_structure(self):
        config = ZcanChannelInitConfig()
        self.assertTrue(issubclass(ZcanChannelInitConfig, ctypes.Structure))
        config.can_type = TYPE_CAN
        self.assertEqual(config.can_type, TYPE_CAN)

    def test_can_fd_open_configures_both_baudrates(self):
        dll = FakeZcanFdDll()
        driver = ZlgVciCanDriver()
        driver._dll = dll

        self.assertTrue(driver.open(41, 0, 0, 1_000_000, can_fd=True, data_baudrate=5_000_000))

        calls = dll.ZCAN_SetValue.calls
        self.assertEqual(calls[0][1:], (b"0/canfd_abit_baud_rate", b"1000000"))
        self.assertEqual(calls[1][1:], (b"0/canfd_dbit_baud_rate", b"5000000"))
        config = dll.ZCAN_InitCAN.calls[0][2]._obj
        self.assertEqual(config.can_type, 1)
        self.assertTrue(driver.can_fd)

    def test_can_fd_send_and_receive_use_fd_api(self):
        dll = FakeZcanFdDll()
        driver = ZlgVciCanDriver()
        driver._dll = dll
        driver.open(41, 0, 0, 1_000_000, can_fd=True, data_baudrate=5_000_000)
        outgoing = CanFrame(id=0x300, data=bytes(range(12)), fd=True, brs=True)

        self.assertTrue(driver.send(outgoing))
        received = driver.receive(10)

        data = ctypes.cast(dll.ZCAN_TransmitFD.calls[-1][1], ctypes.POINTER(ZcanTransmitFdData))
        self.assertEqual(data[0].frame.len, 12)
        self.assertEqual(bytes(data[0].frame.data[:12]), bytes(range(12)))
        self.assertTrue(received.fd)
        self.assertTrue(received.brs)
        self.assertEqual(received.dlc, 12)

    def test_fd_conversion_helpers_preserve_flags_and_payload(self):
        outgoing = CanFrame(id=0x300, data=bytes(range(16)), fd=True, brs=True)
        tx = frame_to_zcan_transmit_fd_data(outgoing)
        rx = ZcanReceiveFdData()
        rx.frame.can_id = tx.frame.can_id
        rx.frame.len = tx.frame.len
        rx.frame.brs = tx.frame.brs
        for index in range(tx.frame.len):
            rx.frame.data[index] = tx.frame.data[index]

        result = zcan_receive_fd_data_to_frame(rx)

        self.assertEqual(result.id, 0x300)
        self.assertEqual(result.dlc, 16)
        self.assertEqual(bytes(result.data[:16]), bytes(range(16)))
        self.assertTrue(result.fd)
        self.assertTrue(result.brs)


if __name__ == "__main__":
    unittest.main()
