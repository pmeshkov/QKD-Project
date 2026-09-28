# Historical whole-device reset helper

`SHUTOFFEOMS.py` has been preserved unchanged from `EOM_scripts/`. It resets **all of NI Dev1**, then writes zero volts to AO0/AO1. A reset interrupts other tasks using the device and discards their resource reservations.

This is a historical recovery tool, not a normal stop button. Use **Stop** or close the active experiment window to request its normal task cleanup. The archived helper is not included in the experiment launcher.
