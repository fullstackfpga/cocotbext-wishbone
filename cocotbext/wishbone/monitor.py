"""
Cocotb 2.0 Compatible Wishbone Monitor and Slave

Updated for cocotb 2.0 compatibility:
- Removed deprecated imports
- Updated signal handling 
- Simplified queue imports
"""

import cocotb
from itertools import repeat
from cocotb.triggers import RisingEdge
from queue import Queue
from .driver import WishboneBase, WBRes

# cocotb 2.0 compatibility
try:
    from cocotb.result import TestFailure
except ImportError:
    # In cocotb 2.0, TestFailure is replaced with exceptions
    class TestFailure(Exception):
        pass


class WishboneSlave(WishboneBase):
    """Wishbone slave that can respond to register reads/writes"""

    def __init__(self, entity, name, clock, memory_map=None, **kwargs):
        super().__init__(entity, name, clock, **kwargs)
        
        # Memory map for register responses
        self.memory_map = memory_map or {}
        self.default_value = kwargs.get('default_value', 0x00000000)
        
        # Initialize slave signals
        self.bus.ack.value = 0
        self.bus.datrd.value = 0
        
        if hasattr(self.bus, "err"):
            self.bus.err.value = 0
        if hasattr(self.bus, "stall"):
            self.bus.stall.value = 0
        if hasattr(self.bus, "rty"):
            self.bus.rty.value = 0
            
        # Start the slave response process
        cocotb.start_soon(self._slave_process())
        
        self.log.info("Wishbone Slave created")

    def set_register(self, address, value):
        """Set a register value in the memory map"""
        self.memory_map[address] = value
        self.log.debug(f"Set register 0x{address:08x} = 0x{value:08x}")

    def get_register(self, address):
        """Get a register value from the memory map"""
        return self.memory_map.get(address, self.default_value)

    async def _slave_process(self):
        """Main slave process that responds to bus transactions"""
        clkedge = RisingEdge(self.clock)
        
        while True:
            await clkedge
            
            # Check for valid transaction
            if (int(self.bus.cyc.value) == 1 and 
                int(self.bus.stb.value) == 1):
                
                # Decode address and operation
                address = int(self.bus.adr.value)
                is_write = int(self.bus.we.value) == 1
                
                if is_write:
                    # Write operation
                    write_data = int(self.bus.datwr.value)
                    self.set_register(address, write_data)
                    self.log.debug(f"WB Write: 0x{address:08x} = 0x{write_data:08x}")
                    
                    # Respond with ACK
                    self.bus.ack.value = 1
                    self.bus.datrd.value = 0
                    
                else:
                    # Read operation
                    read_data = self.get_register(address)
                    self.log.debug(f"WB Read: 0x{address:08x} -> 0x{read_data:08x}")
                    
                    # Respond with data and ACK
                    self.bus.datrd.value = read_data
                    self.bus.ack.value = 1
                
                # Wait one cycle then deassert ACK
                await clkedge
                self.bus.ack.value = 0
                self.bus.datrd.value = 0


# Simple register map for common PCI configuration registers
PCI_CONFIG_REGISTERS = {
    0x00: 0x18950001,  # Vendor ID (0x1895) + Device ID (0x0001) 
    0x04: 0x02800000,  # Command + Status
    0x08: 0x06000001,  # Class Code (Bridge) + Revision ID
    0x0C: 0x00000000,  # Cache Line Size + Latency Timer + Header Type + BIST
    0x10: 0x00000000,  # BAR0
    0x14: 0x00000000,  # BAR1
    0x18: 0x00000000,  # BAR2  
    0x1C: 0x00000000,  # BAR3
    0x20: 0x00000000,  # BAR4
    0x24: 0x00000000,  # BAR5
    0x2C: 0x18950001,  # Subsystem Vendor ID + Subsystem ID
    0x3C: 0x00000000,  # Interrupt Line + Interrupt Pin + Min Grant + Max Latency
}