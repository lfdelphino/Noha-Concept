from pathlib import Path


def open_sensor(pin):
    from gpiozero import DigitalInputDevice
    from gpiozero.pins.lgpio import LGPIOFactory
    # RP1 moved from gpiochip4 to gpiochip0 on newer Pi 5 kernels.
    chips = []
    for label in Path("/sys/bus/gpio/devices").glob("gpiochip*/label"):
        if "pinctrl-rp1" in label.read_text():
            chips.append(int(label.parent.name.removeprefix("gpiochip")))
    if len(chips) != 1:
        raise RuntimeError("Controlador GPIO RP1 não encontrado; execute noha doctor")
    # GPIO3 has a physical pull-up. PIR actively drives high and low.
    return DigitalInputDevice(pin, pull_up=None, active_state=True,
                              pin_factory=LGPIOFactory(chip=chips[0]))

