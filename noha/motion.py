"""The presence sensor: a motion sensor on a GPIO pin, or the Pi camera detecting people."""
from pathlib import Path


def rp1_chip(dev=Path("/dev")):
    # RP1 moved from gpiochip4 to gpiochip0 on newer Pi 5 kernels, and some kernels
    # expose no sysfs label; ask the character device for its label instead.
    import lgpio
    chips = []
    # Newer kernels keep /dev/gpiochip4 as a compatibility symlink to gpiochip0: count devices once.
    for device in sorted({path.resolve() for path in dev.glob("gpiochip*")}):
        number = int(device.name.removeprefix("gpiochip"))
        try:
            handle = lgpio.gpiochip_open(number)
        except Exception:
            continue
        try:
            if "pinctrl-rp1" in lgpio.gpio_get_chip_info(handle)[3]:
                chips.append(number)
        finally:
            lgpio.gpiochip_close(handle)
    if len(chips) != 1:
        raise RuntimeError("Controlador GPIO RP1 não encontrado; execute noha doctor")
    return chips[0]


def open_sensor(pin):
    from gpiozero import DigitalInputDevice
    from gpiozero.pins.lgpio import LGPIOFactory
    # Internal pull-down: an unplugged sensor reads "no motion" instead of floating. GPIO 2/3 cannot be
    # used: the board's 1.8 kΩ I2C pull-ups hold them high even against an HC-SR501 pulling low.
    return DigitalInputDevice(pin, pull_up=False,
                              pin_factory=LGPIOFactory(chip=rp1_chip()))


def camera_problems():
    """What keeps the camera from detecting people, in words for the installer's user; empty when ready."""
    from importlib.util import find_spec
    missing = [name for name in ("numpy", "picamera2", "onnxruntime") if find_spec(name) is None]
    if missing:
        return [f"Faltam bibliotecas ({', '.join(missing)}): sudo apt install python3-picamera2 python3-onnxruntime"]
    from picamera2 import Picamera2
    if not Picamera2.global_camera_info():
        return ["Nenhuma câmera encontrada: confira o cabo flat nas duas pontas e reinicie o Pi"]
    return []


def open_camera(settings, runtime):
    """Person detection with the Pi camera, with the motion sensor's interface (`value`, `close`)."""
    from .camera import CameraSensor
    return CameraSensor(settings, runtime)
