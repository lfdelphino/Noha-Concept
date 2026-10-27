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
