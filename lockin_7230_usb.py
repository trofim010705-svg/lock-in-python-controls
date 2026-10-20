"""
USB-only helper functions for a 7230 lock-in amplifier.

Requires PyVISA and a VISA implementation/driver compatible with the instrument.
"""

import pyvisa

MODEL = "7230"
SERIAL = "14078415"

# VISA resource string for model 7230.
_USB_RESOURCE = f"USB0::0x0A2D::0x0027::{SERIAL}::RAW"

# Create the VISA resource manager.
rm = pyvisa.ResourceManager()


def Connection_Open_USB():
    """Open the USB connection to the configured 7230 instrument."""
    print(f"Opening USB connection to model {MODEL}, serial {SERIAL}")
    inst = rm.open_resource(_USB_RESOURCE)
    return inst


def Inst_Query_Command_USB(inst, sCmd):
    """
    Send a command to the connected instrument over USB.

    Returns:
        (response, status_byte, overload_byte)
    """
    print("Send query command: " + sCmd)
    inst.write_raw(sCmd)
    sResponse = inst.read()

    # The last two characters contain the status and overload bytes.
    nStb = bytes(sResponse[len(sResponse) - 2:len(sResponse) - 1], "utf-8")
    nOvb = bytes(sResponse[len(sResponse) - 1:len(sResponse)], "utf-8")
    nStatusByte = int(nStb[0])

    # Bits 4, 5 and 6 are model-dependent and are not decoded here.
    nStatusByte = nStatusByte & 143
    nOverloadByte = int(nOvb[0])

    # Remove line feed, status byte, overload byte and null terminator.
    sResponse = sResponse[0:len(sResponse) - 4]

    return sResponse, nStatusByte, nOverloadByte


def Connection_Close(inst):
    """Close an open VISA connection."""
    print("Close connection")
    inst.before_close()
    return inst.close()


def Print_Status_Byte(nStatusByte):
    """Print the meanings of the recognized instrument status bits."""
    if nStatusByte & 1:
        print("Command Done")
    if nStatusByte & 2:
        print("Invalid command")
    if nStatusByte & 4:
        print("Command parameter error")
    if nStatusByte & 8:
        print("Reference unlock")
    # Bits 4, 5 and 6 are model-dependent.
    if nStatusByte & 128:
        print("Data Available")


def Print_72XXOverload_Byte(nOverloadByte):
    """Print the meanings of the 72XX output-overload bits."""
    if nOverloadByte & 1:
        print("X(1) output overload")
    if nOverloadByte & 2:
        print("Y(1) output overload")
    if nOverloadByte & 4:
        print("X2 output overload")
    if nOverloadByte & 8:
        print("Y2 output overload")
    if nOverloadByte & 16:
        print("CH1 output overload")
    if nOverloadByte & 32:
        print("CH2 output overload")
    if nOverloadByte & 64:
        print("CH3 output overload")
    if nOverloadByte & 128:
        print("CH4 output overload")


if __name__ == "__main__":
    # Optional smoke test: query the instrument firmware/version.
    inst = Connection_Open_USB()
    try:
        response, status, overload = Inst_Query_Command_USB(inst, "VER")
        Print_Status_Byte(status)
        Print_72XXOverload_Byte(overload)
        if response:
            print("Command response: " + response)
    finally:
        Connection_Close(inst)
