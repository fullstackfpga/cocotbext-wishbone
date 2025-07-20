"""
Cocotb 2.0 Compatible Wishbone Bus Driver

Updated for cocotb 2.0 compatibility:
- Removed deprecated cocotb.binary.BinaryValue
- Removed deprecated cocotb.decorators.public
- Replaced cocotb_bus with native cocotb functionality
- Updated signal handling for cocotb 2.0
"""

import cocotb
from cocotb.triggers import FallingEdge, RisingEdge, Event
from typing import Optional, List, Union, Any

# cocotb 2.0 compatibility
try:
    from cocotb.result import TestFailure
except ImportError:
    # In cocotb 2.0, TestFailure is replaced with exceptions
    class TestFailure(Exception):
        pass


def is_sequence(arg):
    return (not hasattr(arg, "strip") and
            hasattr(arg, "__getitem__") or
            hasattr(arg, "__iter__"))


class WBAux:
    """
    Wishbone Auxiliary Wrapper Class
    wrap meta informations on bus transaction (internal only)
    """
    def __init__(self, sel=None, adr=0, datwr=None, waitStall=0, waitIdle=0, tsStb=0, cti=0, bte=0):
        self.sel = sel
        self.adr = adr
        self.datwr = datwr
        self.waitIdle = waitIdle
        self.waitStall = waitStall
        self.ts = tsStb
        self.cti = cti
        self.bte = bte


class WBOp:
    """
    Wishbone Operations Wrapper Class

    Args:
        adr: address of the operation
        dat: data to write, None indicates a read cycle
        idle: number of clock cycles between asserting cyc and stb
        sel: the selection mask for the operation
        acktimeout: number of maximum clock cycles before asserting ack
        cti: type of cycles done by the operation
        bte: burst type extension, currently only used for signaling inc. burst wrap size
    """
    def __init__(self, adr=0, dat=None, idle=0, sel=0xF, acktimeout=0, cti=0, bte=0):
        self.adr = adr
        self.dat = dat
        self.sel = sel
        self.idle = idle
        self.acktimeout = acktimeout
        self.cti = cti
        self.bte = bte


class WBRes:
    """
    Wishbone Result Wrapper Class.
    What's happend on the bus plus meta information on timing
    """

    def __init__(self, ack=0, sel=None, adr=0, datrd=None, datwr=None, waitIdle=0, waitStall=0, waitAck=0, cti=0, bte=0):
        self.ack = ack
        self.sel = sel
        self.adr = adr
        self.datrd = datrd
        self.datwr = datwr
        self.waitStall = waitStall
        self.waitAck = waitAck
        self.waitIdle = waitIdle
        self.cti = cti
        self.bte = bte


class WishboneBase:
    """Base class for Wishbone bus interfaces"""
    
    _signals = ["cyc", "stb", "we", "adr", "datwr", "datrd", "ack"]
    _optional_signals = ["sel", "err", "stall", "rty", "cti", "bte"]

    def __init__(self, entity, name, clock, width=32, signals_dict=None, **kwargs):
        self.entity = entity
        self.name = name
        self.clock = clock
        self._width = width
        self.log = cocotb.log.getChild(name)
        
        if signals_dict is not None:
            self._signals = signals_dict
            
        # Create bus object with all signals
        self.bus = type('Bus', (), {})()
        
        # Map required signals
        for i, signal_name in enumerate(self._signals):
            if isinstance(signals_dict, list):
                # Use provided signal list directly
                signal_path = signals_dict[i] if i < len(signals_dict) else None
            else:
                # Use naming convention
                signal_path = f"{name}_{signal_name}" if name else signal_name
                
            if signal_path and hasattr(entity, signal_path):
                setattr(self.bus, signal_name, getattr(entity, signal_path))
            else:
                raise AttributeError(f"Required signal {signal_path} not found")
        
        # Map optional signals
        for signal_name in self._optional_signals:
            signal_path = f"{name}_{signal_name}" if name else signal_name
            if hasattr(entity, signal_path):
                setattr(self.bus, signal_name, getattr(entity, signal_path))


class WishboneMaster(WishboneBase):
    """
    Wishbone master
    """
    def __init__(self, entity, name, clock, timeout=None, width=32, **kwargs):
        super().__init__(entity, name, clock, width, **kwargs)
        
        sTo = ", no cycle timeout"
        if timeout is not None:
            sTo = f", cycle timeout is {timeout} clockcycles"
        
        self.busy_event = Event(f"{name}_busy")
        self._timeout = timeout
        self.busy = False
        self._acked_ops = 0
        self._res_buf = []
        self._aux_buf = []
        self._op_cnt = 0
        self._clk_cycle_count = 0
        
        # Initialize signals with immediate values
        self.bus.cyc.value = 0
        self.bus.stb.value = 0
        self.bus.we.value = 0
        self.bus.adr.value = 0
        self.bus.datwr.value = 0

        if hasattr(self.bus, "sel"):
            # Set all sel bits to 1 (cocotb 2.0 compatible)
            self.bus.sel.value = (1 << len(self.bus.sel)) - 1

        if hasattr(self.bus, "cti"):
            self.bus.cti.value = 0

        if hasattr(self.bus, "bte"):
            self.bus.bte.value = 0

        self.log.info(f"Wishbone Master created{sTo}")

    async def _clk_cycle_counter(self):
        """Cycle counter to time bus operations"""
        clkedge = RisingEdge(self.clock)
        self._clk_cycle_count = 0
        while self.busy:
            await clkedge
            self._clk_cycle_count += 1

    async def _open_cycle(self):
        """Open new wishbone cycle"""
        if self.busy:
            self.log.error("Opening Cycle, but WB Driver is already busy. Something's wrong")
            await self.busy_event.wait()
        
        self.busy_event.clear()
        self.busy = True
        cocotb.start_soon(self._read())
        cocotb.start_soon(self._clk_cycle_counter()) 
        self.bus.cyc.value = 1
        self._acked_ops = 0  
        self._res_buf = [] 
        self._aux_buf = []
        self.log.debug(f"Opening cycle, {self._op_cnt} Ops")

    async def _close_cycle(self):
        """Close current wishbone cycle"""
        clkedge = RisingEdge(self.clock)
        count = 0
        last_acked_ops = 0
        
        # Wait for all Operations being acknowledged by the slave
        while self._acked_ops < self._op_cnt:
            if last_acked_ops != self._acked_ops:
                self.log.debug(f"Waiting for missing acks: {self._acked_ops}/{self._op_cnt}")
            last_acked_ops = self._acked_ops    
            
            # Check for timeout when finishing the cycle            
            count += 1
            if self._timeout is not None:
                if count > self._timeout: 
                    raise TestFailure(f"Timeout of {self._timeout} clock cycles reached when waiting for reply from slave")
            await clkedge

        self.busy = False
        self.busy_event.set()
        
        if hasattr(self.bus, "cti"):
            self.bus.cti.value = 0
        if hasattr(self.bus, "bte"):
            self.bus.bte.value = 0
        
        self.bus.cyc.value = 0
        self.log.debug("Closing cycle")
        await clkedge

    async def _wait_stall(self):
        """Wait for stall to be low before continuing (Pipelined Wishbone)"""
        clkedge = RisingEdge(self.clock)
        count = 0
        
        if hasattr(self.bus, "stall"):
            while int(self.bus.stall.value) == 1:
                await clkedge
                count += 1
                if self._timeout is not None:
                    if count > self._timeout: 
                        raise TestFailure(f"Timeout of {self._timeout} clock cycles reached on stall from slave")
            self.log.debug(f"Stalled for {count} cycles")
        return count

    async def _wait_ack(self):
        """Wait for ACK on the bus before continuing (Non pipelined Wishbone)"""
        clkedge = RisingEdge(self.clock)
        count = 0
        
        if hasattr(self.bus, "stall"):
            self.bus.stb.value = 0
            
        if self._acktimeout == 0:
            while not self._get_reply()[0]:
                await clkedge
                count += 1
        else:
            while (not self._get_reply()[0]) and (count < self._acktimeout):
                await clkedge
                count += 1
                
        if (self._acktimeout != 0) and (count >= self._acktimeout):
            raise TestFailure(f"Timeout of {count} clock cycles reached when waiting for acknowledge")

        if not hasattr(self.bus, "stall"):
            self.bus.stb.value = 0
            
        self._acked_ops += 1
        self.log.debug(f"Waited {count} cycles for acknowledge")
        return count

    def _get_reply(self):
        """Get reply from slave"""
        code = 0  # 0 if no reply, 1 for ACK, 2 for ERR, 3 for RTY
        ack = int(self.bus.ack.value) == 1
        
        if ack:
            code = 1
            
        if hasattr(self.bus, "err") and int(self.bus.err.value) == 1:
            if ack:
                raise TestFailure("Slave raised ACK and ERR line")
            ack = True
            code = 2
            
        if hasattr(self.bus, "rty") and int(self.bus.rty.value) == 1:
            if ack:
                raise TestFailure(f"Slave raised {'ACK' if code == 1 else 'ERR'} and RTY line")
            ack = True
            code = 3
            
        return ack, code

    async def _read(self):
        """Reader for slave replies"""
        clkedge = FallingEdge(self.clock)
        while self.busy:
            ack, reply = self._get_reply()
            # valid reply?
            if ack:
                datrd = int(self.bus.datrd.value)
                # append reply and meta info to result buffer
                tmpRes = WBRes(ack=reply, sel=None, adr=None, datrd=datrd, datwr=None, 
                              waitIdle=None, waitStall=None, waitAck=self._clk_cycle_count, cti=None, bte=None)
                self._res_buf.append(tmpRes)
            await clkedge

    async def _drive(self, we, adr, datwr, sel, idle, cti, bte):
        """Drive the Wishbone Master Out Lines"""
        clkedge = RisingEdge(self.clock)
        
        if self.busy:
            # insert requested idle cycles
            if idle is not None:
                idlecnt = idle
                while idlecnt > 0:
                    idlecnt -= 1
                    await clkedge
                    
            # drive outputs
            self.bus.stb.value = 1
            self.bus.adr.value = adr
            
            if hasattr(self.bus, "sel"):
                if sel is not None:
                    self.bus.sel.value = sel
                else:
                    # Set all sel bits to 1 (cocotb 2.0 compatible)
                    self.bus.sel.value = (1 << len(self.bus.sel)) - 1
                    
            if hasattr(self.bus, "cti"):
                self.bus.cti.value = cti
                if hasattr(self.bus, "bte"):
                    self.bus.bte.value = bte
            else:
                if cti != 0:
                    self.log.error("Wishbone bus doesn't support burst cycles")

            self.bus.datwr.value = datwr
            self.bus.we.value = we
            await clkedge
            
            # deal with flow control (pipelined wishbone)
            stalled = await self._wait_stall()
            
            # append operation and meta info to auxiliary buffer
            self._aux_buf.append(WBAux(sel, adr, datwr, stalled, idle, self._clk_cycle_count, cti, bte))
            await self._wait_ack()
            
            self.bus.we.value = 0
            self.bus.datwr.value = 0
        else:
            self.log.error("Cannot drive the Wishbone bus outside a cycle!")

    async def send_cycle(self, arg):
        """
        The main sending routine

        Args:
            list(WishboneOperations)
        """
        clkedge = RisingEdge(self.clock)
        await clkedge
        
        if is_sequence(arg):
            self._op_cnt = len(arg)
            if self._op_cnt < 1:
                self.log.error("List contains no operations to carry out")
            else:
                result = []
                await self._open_cycle()

                for cnt, op in enumerate(arg):
                    if not isinstance(op, WBOp):
                        raise TestFailure("Sorry, argument must be a list of WBOp (Wishbone Operation) objects!")    

                    self._acktimeout = op.acktimeout

                    if op.dat is not None:
                        we = 1
                        dat = op.dat
                    else:
                        we = 0
                        dat = 0
                        
                    await self._drive(we, op.adr, dat, op.sel, op.idle, op.cti, op.bte)
                    
                    if op.sel is not None:
                        self.log.debug(f"#{cnt:3} WE: {we} ADR: 0x{op.adr:08x} DAT: 0x{dat:08x} SEL: 0x{op.sel:1x} IDLE: {op.idle:3} CTI: 0x{op.cti:03x} BTE: 0x{op.bte:02x}")
                    else:
                        self.log.debug(f"#{cnt:3} WE: {we} ADR: 0x{op.adr:08x} DAT: 0x{dat:08x} SEL: None  IDLE: {op.idle:3} CTI: 0x{op.cti:03x} BTE: 0x{op.bte:02x}")

                await self._close_cycle()

                # do pick and mix from result- and auxiliary buffer so we get all operation and meta info
                for res, aux in zip(self._res_buf, self._aux_buf):
                    res.datwr = aux.datwr
                    res.sel = aux.sel
                    res.adr = aux.adr
                    res.waitIdle = aux.waitIdle
                    res.waitStall = aux.waitStall
                    res.waitAck -= aux.ts
                    res.cti = aux.cti
                    res.bte = aux.bte
                    result.append(res)

            return result
        else:
            raise TestFailure("Sorry, argument must be a list of WBOp (Wishbone Operation) objects!")