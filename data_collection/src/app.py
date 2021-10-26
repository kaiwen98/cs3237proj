# -*- coding: utf-8 -*-
"""
TI CC2650 SensorTag
-------------------

Adapted by Ashwin from the following sources:
 - https://github.com/IanHarvey/bluepy/blob/a7f5db1a31dba50f77454e036b5ee05c3b7e2d6e/bluepy/sensortag.py
 - https://github.com/hbldh/bleak/blob/develop/examples/sensortag.py

"""
import asyncio
import platform
import struct
import bleak
import os
import signal
import sys

from bleak import (
    BleakClient,
    discover
)
from commons.commons import (
    BLE_ADDR_LIST,
    BLE_ADDR_TO_NAME
)
from utils.ble_utils import (
    discover_sensors
)

from utils.utils import (
    appendDataToDataframe,
    getDataframeFromDatalist, 
    loadDataframeFromCsv, 
    saveDataframeToCsv,
    getTimeStamp
)

list_of_acc = []
list_of_mag = []
list_of_gyro = []

ble_client_dict = {}

ble_queue = asyncio.Queue()

time_to_record: int

class Service:
    """
    Here is a good documentation about the concepts in ble;
    https://learn.adafruit.com/introduction-to-bluetooth-low-energy/gatt

    In TI SensorTag there is a control characteristic and a data characteristic which define a service or sensor
    like the Light Sensor, Humidity Sensor etc

    Please take a look at the official TI user guide as well at
    https://processors.wiki.ti.com/index.php/CC2650_SensorTag_User's_Guide
    """

    def __init__(self):
        self.data_uuid = None
        self.ctrl_uuid = None


class Sensor(Service):

    def callback(self, sender: int, data: bytearray):
        raise NotImplementedError()

    async def start_listener(self, client, *args):
        # start the sensor on the device
        write_value = bytearray([0x01])
        await client.write_gatt_char(self.ctrl_uuid, write_value)

        # listen using the handler
        await client.start_notify(self.data_uuid, self.callback)


class MovementSensorMPU9250SubService:

    def __init__(self):
        self.bits = 0

    def enable_bits(self):
        return self.bits

    def cb_sensor(self, data):
        raise NotImplementedError


class MovementSensorMPU9250(Sensor):
    GYRO_XYZ = 7
    ACCEL_XYZ = 7 << 3
    MAG_XYZ = 1 << 6
    ACCEL_RANGE_2G  = 0 << 8
    ACCEL_RANGE_4G  = 1 << 8
    ACCEL_RANGE_8G  = 2 << 8
    ACCEL_RANGE_16G = 3 << 8

    def __init__(self):
        super().__init__()
        self.data_uuid = "f000aa81-0451-4000-b000-000000000000"
        self.ctrl_uuid = "f000aa82-0451-4000-b000-000000000000"
        self.ctrlBits = 0

        self.sub_callbacks = []

    def register(self, cls_obj: MovementSensorMPU9250SubService):
        self.ctrlBits |= cls_obj.enable_bits()
        self.sub_callbacks.append(cls_obj.cb_sensor)

    async def start_listener(self, client, *args):
        # start the sensor on the device
        await client.write_gatt_char(self.ctrl_uuid, struct.pack("<H", self.ctrlBits))

        # listen using the handler
        await client.start_notify(self.data_uuid, self.callback)

    def callback(self, sender: int, data: bytearray):
        unpacked_data = struct.unpack("<hhhhhhhhh", data)
        for cb in self.sub_callbacks:
            cb(unpacked_data)


class AccelerometerSensorMovementSensorMPU9250(MovementSensorMPU9250SubService):
    def __init__(self):
        super().__init__()
        self.bits = MovementSensorMPU9250.ACCEL_XYZ | MovementSensorMPU9250.ACCEL_RANGE_4G
        self.scale = 8.0/32768.0 # TODO: why not 4.0, as documented? @Ashwin Need to verify
        self.readings : list

    def cb_sensor(self, data):
        '''Returns (x_accel, y_accel, z_accel) in units of g'''
        rawVals = data[3:6]
        # print("[MovementSensor] Accelerometer:", tuple([ v*self.scale for v in rawVals ]))
        self.readings = [getTimeStamp(), tuple([ v*self.scale for v in rawVals ])]
        

class MagnetometerSensorMovementSensorMPU9250(MovementSensorMPU9250SubService):
    def __init__(self):
        super().__init__()
        self.bits = MovementSensorMPU9250.MAG_XYZ
        self.scale = 4912.0 / 32760
        self.readings : list
        # Reference: MPU-9250 register map v1.4

    def cb_sensor(self, data):
        '''Returns (x_mag, y_mag, z_mag) in units of uT'''
        rawVals = data[6:9]
        # print("[MovementSensor] Magnetometer:", tuple([ v*self.scale for v in rawVals ]))
        self.readings = [getTimeStamp(), tuple([ v*self.scale for v in rawVals ])]
        

class GyroscopeSensorMovementSensorMPU9250(MovementSensorMPU9250SubService):
    def __init__(self):
        super().__init__()
        self.bits = MovementSensorMPU9250.GYRO_XYZ
        self.scale = 500.0/65536.0
        self.readings : list

    def cb_sensor(self, data):
        '''Returns (x_gyro, y_gyro, z_gyro) in units of degrees/sec'''
        rawVals = data[0:3]
        # print("[MovementSensor] Gyroscope:", tuple([ v*self.scale for v in rawVals ]))
        self.readings = [getTimeStamp(), tuple([ v*self.scale for v in rawVals ])]
        

class OpticalSensor(Sensor):
    def __init__(self):
        super().__init__()
        self.data_uuid = "f000aa71-0451-4000-b000-000000000000"
        self.ctrl_uuid = "f000aa72-0451-4000-b000-000000000000"

    def callback(self, sender: int, data: bytearray):
        raw = struct.unpack('<h', data)[0]
        m = raw & 0xFFF
        e = (raw & 0xF000) >> 12
        print("[OpticalSensor] Reading from light sensor:", 0.01 * (m << e))



class HumiditySensor(Sensor):
    def __init__(self):
        super().__init__()
        self.data_uuid = "f000aa21-0451-4000-b000-000000000000"
        self.ctrl_uuid = "f000aa22-0451-4000-b000-000000000000"

    def callback(self, sender: int, data: bytearray):
        (rawT, rawH) = struct.unpack('<HH', data)
        temp = -40.0 + 165.0 * (rawT / 65536.0)
        RH = 100.0 * (rawH/65536.0)
        print(f"[HumiditySensor] Ambient temp: {temp}; Relative Humidity: {RH}")


class BarometerSensor(Sensor):
    def __init__(self):
        super().__init__()
        self.data_uuid = "f000aa41-0451-4000-b000-000000000000"
        self.ctrl_uuid = "f000aa42-0451-4000-b000-000000000000"

    def callback(self, sender: int, data: bytearray):
        (tL, tM, tH, pL, pM, pH) = struct.unpack('<BBBBBB', data)
        temp = (tH*65536 + tM*256 + tL) / 100.0
        press = (pH*65536 + pM*256 + pL) / 100.0
        print(f"[BarometerSensor] Ambient temp: {temp}; Pressure Millibars: {press}")


class LEDAndBuzzer(Service):
    """
        Adapted from various sources. Src: https://evothings.com/forum/viewtopic.php?t=1514 and the original TI spec
        from https://processors.wiki.ti.com/index.php/CC2650_SensorTag_User's_Guide#Activating_IO

        Codes:
            1 = red
            2 = green
            3 = red + green
            4 = buzzer
            5 = red + buzzer
            6 = green + buzzer
            7 = all
    """

    def __init__(self):
        super().__init__()
        self.data_uuid = "f000aa65-0451-4000-b000-000000000000"
        self.ctrl_uuid = "f000aa66-0451-4000-b000-000000000000"

    async def notify(self, client, code):
        # enable the config
        write_value = bytearray([0x01])
        await client.write_gatt_char(self.ctrl_uuid, write_value)

        # turn on the red led as stated from the list above using 0x01
        write_value = bytearray([code])
        await client.write_gatt_char(self.data_uuid, write_value)


async def run(address, postfix, queue):
    ble_client = BleakClient(address, timeout = 10.0)
    await ble_queue.put(ble_client)

    async with ble_client as client:
        x = await client.is_connected()
        print("Connected!")
        acc_sensor = AccelerometerSensorMovementSensorMPU9250()
        gyro_sensor = GyroscopeSensorMovementSensorMPU9250()
        magneto_sensor = MagnetometerSensorMovementSensorMPU9250()

        movement_sensor = MovementSensorMPU9250()
        movement_sensor.register(acc_sensor)
        movement_sensor.register(gyro_sensor)
        movement_sensor.register(magneto_sensor)
        await movement_sensor.start_listener(client)    
        cntr = 0

        acc_sensor_readings_list = []
        gyro_sensor_readings_list = []
        magneto_sensor_readings_list = []
        while True:
            await asyncio.sleep(0.5)
            cntr += 1
            if cntr == 10:
                async with asyncio.Lock():
                    datalist = \
                        {
                            "postfix" : postfix,
                            "acc" : acc_sensor_readings_list,
                            "gyro" : gyro_sensor_readings_list,
                            "mag" : magneto_sensor_readings_list
                        }
                    
                    print("here", datalist)

                    await queue.put(datalist)
                    print("1")
                    await queue.join()
                    print("wait")
                cntr = 0
                acc_sensor_readings_list = []
                gyro_sensor_readings_list = []
                magneto_sensor_readings_list = []
            else:
                acc_sensor_readings_list.append(acc_sensor.readings)
                gyro_sensor_readings_list.append(gyro_sensor.readings)
                magneto_sensor_readings_list.append(magneto_sensor.readings)

            
async def controller(queue):
    classification = sys.argv[1]
    time_to_test = float(sys.argv[2])
    df = None


    if os.path.exists("out.csv"):
        df = loadDataframeFromCsv("out.csv")
        print(df)

    datetime_now = getTimeStamp()

    while(1):
        
        if (getTimeStamp() - datetime_now) >= time_to_test:
            print("Data collection done!")
            return

        print("Time left --- %d\n" % (time_to_test - (getTimeStamp() - datetime_now)))


        datalists = []
        postfixes = []
        print("hi")
        for i in range(len(BLE_ADDR_LIST)):
            datalist = await queue.get()
            print("there", datalist)
            postfixes.append(datalist.pop("postfix"))
            datalists.append(datalist)
            
        
        if df is None:
            df = getDataframeFromDatalist(datalists, postfixes, classification)
        else: 
            df = appendDataToDataframe(df, datalists, postfixes, classification)

        for i in range(len(BLE_ADDR_LIST)):
            queue.task_done()

        saveDataframeToCsv(df, "out.csv")
        
        

        await asyncio.sleep(1)




async def main(queue):
    while not await discover_sensors():
        print("waiting")
        await asyncio.sleep(1.0)

    await asyncio.gather(*(run(address, BLE_ADDR_TO_NAME[address], queue) for address in BLE_ADDR_LIST), controller(queue))

async def handleException():
    while not ble_queue.empty():
        client = await ble_queue.get()
        await client.disconnect()

async def handleException_(loop):
    loop = asyncio.get_event_loop()
    loop.run_until_complete(handleException())
    loop.close()

if __name__ == "__main__":
    """
    To find the address, once your sensor tag is blinking the green led after pressing the button, run the discover.py
    file which was provided as an example from bleak to identify the sensor tag device
    """
    data_queue = asyncio.Queue(maxsize=2)

    if len(sys.argv) < 3:
        print("Enter the category! and time! ")
        sys.exit()
    

    # KW: https://github.com/hbldh/bleak/issues/345 
    
    print(
        "Collecting ground truth labels for category %s --- %s seconds!" % (sys.argv[1], sys.argv[2])
    )
    os.environ["PYTHONASYNCIODEBUG"] = str(1)
    loop = asyncio.get_event_loop()

    try:
        loop.run_until_complete(main(data_queue))
        loop.run_forever()
        loop.set_exception_handler(handleException_)
    except:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(handleException())
        loop.close()


