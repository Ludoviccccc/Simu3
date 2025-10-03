from os import altsep
#===============================================================================
# This model represents a memory hierarchy with
# - 2 levels of cache (L1 local, L2 shared)
# - no cache sharing policy (local caches may become inconsistent with respect 
#   to main memory)
# - a DDR memory implementing some simple optimization features (see the DDR class)
# - an interconnect
# The number of cores, levels of cache, characteristics of the cache (number of ways,...)
# are parameters and can be modified.
#===============================================================================

import random
import heapq
from enum import Enum, auto

# ==========================================================
# Global clock
# ==========================================================
global_cycle = 0


# -----------------------------------------------------
# CacheLine: Represents a single cache line in the cache hierarchy
# -----------------------------------------------------
# We are using n-way associative cache:
# - Each set contains n cache lines with a tag entry.
# - Each line contains m bytes.
#
# A memory Address is structured as follows:
# +----------------+-----------+-----------+
# |     Tag        |   Index   |  Offset   |
# +----------------+-----------+-----------+
# where
#
# (Used to find blk) (Set selection) (Byte in block)
# "Block" and "line" are used interchangeably.
# Cache Structure:
# Set 0: [Block 0] [Block 1] [Block 2] [Block 3]  ← 4-way associativity (n=4)
# Set 1: [Block 0] [Block 1] [Block 2] [Block 3]
# ...
# Set N: [Block 0] [Block 1] [Block 2] [Block 3]

class CacheLine:
    def __init__(self):
        self.valid = False       # Indicates if this line holds valid data
        self.tag = None          # Tag of the data block
        self.dirty = False       # Indicates if the line has been written to (for write-back)

# Implements Pseudo-LRU (PLRU) replacement policy for N-way set associative caches
# The pseudoi-LRU is used to determine the bock to replace in case of cache miss.
# A binary tree is used to implement the PLRU algorithm. There is one tree per set.
# For a 4-way cache, 3 bits are used to determine the block to select.
#
#      Bit 0 (Root)
#     /           \
#   Bit 1         Bit 2
#   /   \         /   \
# Block0 Block1 Block2 Block3
#
# Each node of the tree contains a direction (left=0, right=1) that indicates
# the path to follow to find the next pLRU entry.
class PLRU:
    def __init__(self, ways):
        self.bits = [0] * (ways - 1)  # Tree structure to track usage
        self.ways = ways

    # Update the binary tree in case of a hit
    # The bits in the tree are modified to point "away" from this entry
    # (which is the MRU)
    def update_on_access(self, way):
        idx = 0
        num_levels = self.ways.bit_length() - 1
        for level in range(num_levels):
            # Select direction according to the way
             # (e.g., way=3=0b101 in a 4-way cache => direction = 1 (right subtree), 0 (left subtree)
            direction = (way >> (num_levels - 1 - level)) & 1
            self.bits[idx] = 1-direction # Point to the opposite direction
            idx = (idx << 1)+ 1 + direction

    # Compute the next victim (the pLRU)
    # The block is selected by traversing the tree according
    # to the directions given by each bit.
    def get_victim(self):
        idx = 0
        way = 0
        for level in range(self.ways.bit_length() - 1):
            direction = self.bits[idx]
            way = (way << 1) | direction
            idx = ( idx << 1) + 1 + direction
        return way

# ---------------------------------------------------------
# Represents a memory access request (either read or write)
# ---------------------------------------------------------
class MemoryRequest:
    def __init__(self, core_id, time, req_type, addr, callback=None):
        self.core_id = core_id
        self.time = time              # Time of request
        self.req_type = req_type      # Type of request 'read' or 'write'
        self.addr = addr
        self.callback = callback      # Callback function to signal read completion
        self.completion_time = -1     # When the request is expected to complete

    def __lt__(self, other):
        # Prioritize based on completion time for scheduling
        return self.time < other.time

    def __str__(self):
        return f"<req: {self.req_type.upper()}@{self.addr} from core {self.core_id} >"

# ---------------------------------------------------------
# Interconnect model between CPU cores and DDR, with bandwidth and latency
# ---------------------------------------------------------
# Behaviour :
# - Each request takes at least some base delay to be served.
# - A request may be delayed if the interconnect bandwidth has been "used"
# The interconnect cannot serve more than "bandwidth" requests in one cycle.
# Note
# - Using a heapqueue ensures that all items are and remain sorted
#   according to their ready_time (and req)
class Interconnect:
    def __init__(self, memory_controller, delay=5, bandwidth=4):
        self.memory_controller = memory_controller
        self.queue = []               # Queue of pending memory requests (ready_time, request)
        self.delay = delay            # Base delay before forwarding to DDR controller
        self.bandwidth = bandwidth    # Max number of requests per cycle
        self.cycle = 0

    # Push a request into the interconnect queue.
    # We push the tuple (ready_time, request) where ready_time is the earliest
    # time at which the request may be served by the interconnect.
    def request(self, req):
        global global_cycle

        # Add a random component to the delay for more realistic simulation
        ready_time = self.cycle + self.delay + random.randint(0, 2)
        heapq.heappush(self.queue, (ready_time, req))

        print(f"{global_cycle}: [Interconnect] Request {req.req_type.upper()}@{req.addr} from core {req.core_id} queued, to be released at {ready_time}")

    # Process the interconnect's current cycle
    def tick(self):
        global global_cycle
        processed = 0
        requests_to_forward = []

        # Identify requests ready to be forwarded to the memory controller, respecting bandwidth
        while self.queue and self.queue[0][0] <= self.cycle and processed < self.bandwidth:
            ready_time, req = heapq.heappop(self.queue)
            requests_to_forward.append(req)
            processed += 1

        # Forward the selected requests to the memory controller
        for req in requests_to_forward:
            print(f"{global_cycle}: [Interconnect] Request {req} sent to memory controller")
            self.memory_controller.request(req)

        self.cycle += 1


# ---------------------------------------------------------
# DDR Memory Controller Model
# Arbitrates and schedules requests for the DDR memory
# ---------------------------------------------------------
class DDRMemoryController:
    def __init__(self, ddr_model, tRCD=15, tRP=15, tCAS=15, tRC=30, tWR=15, tRTP=8, tCCD=4):
        self.ddr = ddr_model
        self.queue = []  # Requests waiting to be scheduled by the controller
        self.scheduled_ddr_requests = [] # Requests passed to DDR, waiting for completion
        self.cycle = 0

        # DDR timing parameters (example values)
        self.tRCD = tRCD    # Row to Column Delay
        self.tRP = tRP      # Row Precharge
        self.tCAS = tCAS    # Column Access Strobe latency
        self.tRC = tRC      # Row Cycle time
        self.tWR = tWR      # Write Recovery Time
        self.tRTP = tRTP    # Read to Precharge Time
        self.tCCD = tCCD    # Column to Column Delay

        # State to track for arbitration (from the paper's strategy)
        self.last_command_time = {} # Tracks when a bank was last commanded
        self.bank_open_row = [None] * self.ddr.num_banks
        self.bank_precharge_complete_time = [0] * self.ddr.num_banks
        self.last_access_command = {} # To track RD/WR transition penalties
        self.last_access_addr = {} # To track the last accessed address for a core

    # Enqueue a request
    def request(self, req):
        global global_cycle
        print(f"{global_cycle}: [DDR controller] request queued: {req.req_type.upper()}@{req.addr}")
        heapq.heappush(self.queue, (req.time, req)) # Store with original arrival time for fairness

    def tick(self):
        global global_cycle
        # First, complete any requests that DDR has finished processing
        self._complete_ddr_requests()

        # Then, schedule a new request if possible
        self._schedule_next_request()

        self.cycle += 1


    def _complete_ddr_requests(self):
        global global_cycle
        # Requests are completed as soon as DDR signals they are done.
        completed = []
        for req_info in self.scheduled_ddr_requests:
            req = req_info['request']
            if req.completion_time <= self.cycle:
                if req.req_type == 'read':
                    _ = self.ddr.memory.get(req.addr, 0) # Read value from DDR model
                    print(f"{global_cycle}: [DDR controller] READ@{req.addr} complete")
                    if req.callback:
                        req.callback()
                elif req.req_type == 'write':
                    print(f"{global_cycle}: [DDR controller] WRITE@{req.addr} complete")

                completed.append(req_info)

        for req_info in completed:
            self.scheduled_ddr_requests.remove(req_info)


    def _schedule_next_request(self):
        global global_cycle
        if not self.queue:  # No request, return
            return

        # Apply arbitration strategy:
        # 1. Read prioritization
        # 2. Opened row prioritization
        # 3. RD/WR batching (simplified by favoring row hits and avoiding bank conflicts)
        # 4. Older commands (handled by initial sorting in `self.queue` which is a min-heap based on arrival time)

        # Candidates for scheduling
        candidates = []
        for _, req in self.queue:
            bank = self.ddr._get_bank(req.addr)
            row = self.ddr._get_row(req.addr)

            # Check if bank is available (not in precharge)
            if self.bank_precharge_complete_time[bank] > self.cycle:
                continue

            # Check for intra-bank constraints (e.g., tRC for ACT commands, tCCD for consecutive RD/WR to same bank)
            # This is a simplified check for illustration
            last_cmd_time = self.last_command_time.get(bank, -self.tRC) # Default if no previous command
            if self.cycle < last_cmd_time + self.tCCD: # Basic command-to-command delay
                 continue

            candidates.append(req)

        if not candidates:
            print(f"{global_cycle}: [DDR controller] No suitable candidates for scheduling this cycle.")
            return

        # Sort candidates based on priority rules (simplified scoring for demonstration)
        # We want to prioritize:
        # 1. Row hits
        # 2. Reads over Writes
        # 3. Older requests (handled by min-heap property of self.queue)
        candidates.sort(key=lambda req: (
            0 if self.bank_open_row[self.ddr._get_bank(req.addr)] == self.ddr._get_row(req.addr) else 1, # Row hit first
            0 if req.req_type == 'read' else 1, # Reads before writes
            req.time # Oldest request if other criteria are equal
        ))

        best_req = candidates[0]
        bank = self.ddr._get_bank(best_req.addr)
        row = self.ddr._get_row(best_req.addr)

        # Calculate actual delay for the request
        delay = self.ddr.base_latency
        row_status = "ROW HIT"
        if self.bank_open_row[bank] == row:
            print(f"{global_cycle}: [DDR] ROW HIT@{best_req.addr} for bank {bank} ")
            delay = self.ddr.row_hit_latency
        else:
            print(f"{global_cycle}: [DDR] ROW MISS@{best_req.addr} for bank {bank} ")
            delay = self.tRP + self.tRCD + self.tCAS # ACT (tRCD) + PRE (tRP) + CAS
            row_status = "ROW MISS"
            self.bank_precharge_complete_time[bank] = self.cycle + self.tRP # Bank busy during precharge
            self.bank_open_row[bank] = row # Update opened row for the bank

        # Add transition penalties (WR->RD or RD->WR)
        # From Figure 3.10 and 3.11: WR->RD adds twTR, RD->WR adds WL (Write Latency) + 2 cycles
        # Assuming WR is tCAS + tWR and RD is tCAS
        if bank in self.last_access_command:
            last_cmd_type = self.last_access_command[bank]
            if last_cmd_type == 'write' and best_req.req_type == 'read':
                # Simplified: add twR as turnaround penalty for WR->RD
                delay += self.tWR # tWTR for actual paper value
                print(f"{global_cycle}: [DDR] Applying WR->RD transition penalty for Bank {bank}")
            elif last_cmd_type == 'read' and best_req.req_type == 'write':
                # Simplified: add tWR (Write Latency) + 2 cycles for RD->WR
                delay += self.tWR + 2
                print(f"{global_cycle}: [DDR] Applying RD->WR transition penalty for Bank {bank}")

        completion_time = self.cycle + delay

        # Update controller's state after scheduling
        self.last_command_time[bank] = self.cycle
        self.last_access_command[bank] = best_req.req_type
        self.last_access_addr[bank] = best_req.addr

        # Remove the request from the controller's queue
        for i, (time, req) in enumerate(self.queue):
            if req == best_req:
                self.queue.pop(i)
                break
        heapq.heapify(self.queue) # Re-heapify after pop

        print(f"{global_cycle}: [DDR controller] Scheduling {best_req.req_type.upper()}@{best_req.addr} via Controller")
        print(f"{global_cycle}: [DDR controller] Bank {bank}, Row {row} | {row_status} | Calculated Delay: {delay} | Completion at Cycle {completion_time}")

        # Pass the request to the DDR
        best_req.time = self.cycle # Update request time to when it's issued to DDR
        best_req.completion_time = completion_time
        self.ddr.request(best_req) # DDR will now track its internal completion
        self.scheduled_ddr_requests.append({'request': best_req, 'bank': bank, 'row': row, 'status': row_status})


class DDRState(Enum):
    IDLE = auto()
    ACTIVATE_BANK_ROW = auto()
    WRITING = auto()
    READING = auto()
    PRECHARGING = auto()

#---------------------------------------------------------
# DDR Memory Model
#---------------------------------------------------------
class DDRMemory:
    def __init__(self, num_banks=4):
        self.num_banks = num_banks
        self.memory = {} # Actual data storage (addr -> value)
        self.cycle = 0
        # [TODO] Provide real latency values
        self.base_latency = 0
        self.row_hit_latency = 0

        # State machine for each bank
        self.bank_states = [DDRState.IDLE] * num_banks
        self.bank_timers = [0] * num_banks # Time until next state transition
        self.bank_open_row = [None] * num_banks # Currently open row in each bank
        self.bank_active_requests = [None] * num_banks # Request currently being serviced by a bank

        self.scheduled_completions = [] # Requests whose data is ready to be returned

    def _get_bank(self, addr):
        return addr % self.num_banks

    def _get_row(self, addr):
        return addr // 16 # Example: each row covers 16 addresses (line_size is 4, so 4 cache lines per row for a 4-line_size cache)

    # Request from controller to DDR (e.g., ACT, RD, WR, PRE)
    def request(self, req):
        global global_cycle

        bank = self._get_bank(req.addr)
        row = self._get_row(req.addr)

        # Simplified state transitions (see Figure 3.9 in Mascarenas-Gonzalez thesis)
        current_state = self.bank_states[bank]
        print(f"{global_cycle}: [DDR] Bank {bank} receives {req.req_type.upper()} for Row {row}. Current state: {current_state.name}")

        if req.req_type == 'read':
            if current_state == DDRState.IDLE or self.bank_open_row[bank] != row:
                # Need to ACTIVATE first (handled by controller for actual delay)
                # For this simplified FSM, we assume controller has done the ACT/PRE
                self.bank_states[bank] = DDRState.READING
                self.bank_timers[bank] = req.completion_time
                self.bank_open_row[bank] = row
                print(f"{global_cycle}: [DDR] Bank {bank} transition: IDLE or row change -> READING scheduled at {req.completion_time}")

            elif current_state == DDRState.ACTIVATE_BANK_ROW or current_state == DDRState.READING:
                self.bank_states[bank] = DDRState.READING
                self.bank_timers[bank] = req.completion_time
                print(f"{global_cycle}: [DDR] Bank {bank} transition: ACTIVATE_BANK_ROW/READING -> READING scheduled at {req.completion_time}")
                
            else:
                print(f"{global_cycle}: [DDR] ERROR: Cannot READ from Bank {bank} in state {current_state.name}")

        elif req.req_type == 'write':
            if current_state == DDRState.IDLE or self.bank_open_row[bank] != row:
                self.bank_states[bank] = DDRState.WRITING
                self.bank_timers[bank] = req.completion_time
                self.bank_open_row[bank] = row
                print(f"{global_cycle}: [DDR] Bank {bank} transition: IDLE -> WRITING  scheduled at {req.completion_time}")

            elif current_state == DDRState.ACTIVATE_BANK_ROW or current_state == DDRState.WRITING:
                self.bank_states[bank] = DDRState.WRITING
                self.bank_timers[bank] = req.completion_time
                print(f"{global_cycle}: [DDR] Bank {bank} transition: ACTIVATE_BANK_ROW/WRITING -> WRITING  scheduled at {req.completion_time}")
            else:
                print(f"{global_cycle}: [DDR] ERROR: Cannot WRITE to Bank {bank} in state {current_state.name}")

        # Store the request with its completion time for processing
        heapq.heappush(self.scheduled_completions, (req.completion_time, req))


    # DDR's internal tick, handling state transitions and data completion
    def tick(self):
        global global_cycle
        # Process scheduled completions
        while self.scheduled_completions and self.scheduled_completions[0][0] <= self.cycle:
            completion_time, req = heapq.heappop(self.scheduled_completions)
            bank = self._get_bank(req.addr)

            if req.req_type == 'read':
                pass # Operation is done by controller   
            elif req.req_type == 'write':
                pass #  Operation is done by controller 

            # Update bank state after completion
            if self.bank_states[bank] == DDRState.READING or self.bank_states[bank] == DDRState.WRITING:
                # After a read/write, it implicitly goes to ACTIVATE_BANK_ROW, ready for more column access or PRE
                self.bank_states[bank] = DDRState.ACTIVATE_BANK_ROW
                self.bank_timers[bank] = 0 # Ready for next command
                print(f"{global_cycle}: [DDR] Bank {bank} access completion, transition: READING/WRITING -> ACTIVATE_BANK_ROW")

        # Update FSM timers for each bank
        for i in range(self.num_banks):
            if self.bank_timers[i] > self.cycle:
                # Timer still running, nothing to do for this cycle
                pass
            elif self.bank_states[i] == DDRState.ACTIVATE_BANK_ROW:
                # If a bank is active and its previous command timer expired, it's ready for another
                # READ/WRITE or can be PRECHARGED by the controller.
                pass # Stays in ACTIVATE_BANK_ROW until controller issues next command
            elif self.bank_states[i] == DDRState.PRECHARGING and self.bank_timers[i] <= self.cycle:
                self.bank_states[i] = DDRState.IDLE
                self.bank_open_row[i] = None
                print(f"{global_cycle}: [DDR] Bank {i} transition: PRECHARGING -> IDLE")

        self.cycle += 1

#---------------------------------------
# Models one level in the cache hierarchy
#----------------------------------------
class CacheLevel:
    def __init__(self, level_name, core_id, size, line_size, assoc, memory=None, write_back=True, write_allocate=True):
        self.level = level_name
        self.core_id = core_id
        self.line_size = line_size
        self.assoc = assoc
        self.num_sets = (size // line_size) // assoc
        self.sets = [[CacheLine() for _ in range(assoc)] for _ in range(self.num_sets)]
        self.plru_trees = [PLRU(assoc) for _ in range(self.num_sets)]
        self.memory = memory        # Could be DDR or next cache level (now it's interconnect)
        self.lower = None           # Lower level cache
        self.write_back = write_back
        self.write_allocate = write_allocate
        self.hits = 0
        self.misses = 0

    # Extract the set index from the address
    #  addr = [ tag ][ idx ][ offset ]
    def _index(self, addr):
        return (addr // self.line_size) % self.num_sets

    # Extract the tag from the address
    #  addr = [ tag ][ idx ][ offset ]
    def _tag(self, addr):
        return addr // (self.line_size * self.num_sets)

    # Handles cache read request
    def read(self, addr, callback):
        global global_cycle

        index = self._index(addr)
        tag = self._tag(addr)
        cache_set = self.sets[index]
        plru = self.plru_trees[index]
				
        print(f"{global_cycle}: [Cache {self.level}] READ@{addr} from {self.core_id}")

        # Seach the tag in the cache set
        for i, line in enumerate(cache_set):
            if line.valid and line.tag == tag:
                # There is a hit.
                # Trace event
                print(f"{global_cycle}: [Cache {self.level}] READ HIT@{addr} from {self.core_id}")
                
                # Count hits
                self.hits += 1
                
                # Update the pLRU tree to point away from the MRU
                plru.update_on_access(i)

                callback()

                return

        # Cache miss...
        # Trace event
        print(f"{global_cycle}: [Cache {self.level}] READ MISS@{addr} from {self.core_id}")
        
        # Count misses
        self.misses += 1
        
        # Choose victim line using PLRU and fetch from lower memory
        victim_idx = plru.get_victim()
        victim_line = cache_set[victim_idx]

        # If the victim line is valid and dirty, we have to write the data to
        # the next level of memory before loading the cache entry with the
        # data.
        def lower_cb():
            # Write evicted data if dirty
            if victim_line.valid and victim_line.dirty and self.write_back:
                victim_addr = ((victim_line.tag * self.num_sets) + index) * self.line_size
                if self.lower:
                    self.lower.write(victim_addr) 
                elif self.memory: # If L2, send to interconnect
                    self.memory.request(MemoryRequest(self.core_id, self.memory.cycle, 'write', victim_addr, callback=None))
            # Now that we have written the data to the next memory level, the
            # cache entry is updated
            victim_line.valid = True
            victim_line.tag = tag
            victim_line.dirty = False
            plru.update_on_access(victim_idx)
            # Signal that the read operation is complete.
            callback()

        # Forward the read request to the lower-level cache (if any)
        if self.lower:
            self.lower.read(addr, lower_cb)
        # Or to Interconnect (which then sends to DDR Controller)
        elif self.memory:
            self.memory.request(MemoryRequest(self.core_id, self.memory.cycle, 'read', addr, lower_cb))

    # Handles cache write request
    def write(self, addr):
        global global_cycle

        index = self._index(addr)
        tag = self._tag(addr)
        cache_set = self.sets[index]
        plru = self.plru_trees[index]

        print(f"{global_cycle}: [Cache {self.level}] WRITE@{addr} from {self.core_id}")

        for i, line in enumerate(cache_set):
            if line.valid and line.tag == tag:
                # There is a cache hit
                # Trace event
                print(f"{global_cycle}: [Cache {self.level}] WRITE HIT@{addr} from {self.core_id}")

                # Count hits
                self.hits += 1

                # If the cache is write-back, the data will be written to
                # memory when evicted, so it is marked "dirty"
                line.dirty = True if self.write_back else False
                plru.update_on_access(i)
                # If the cache is write-through, the write operation is
                # propagated to the lower levels of the memory hierarchy
                if not self.write_back:
                    if self.lower:
                        self.lower.write(addr)
                    elif self.memory: # If L2, send to interconnect
                        self.memory.request(MemoryRequest(self.core_id, self.memory.cycle, 'write', addr))
                
                return

        # There is a cache miss...
        # Trace event
        print(f"{global_cycle}: [Cache {self.level}] WRITE MISS@{addr} from {self.core_id}")
        
        # Count misses
        self.misses += 1

        if self.write_allocate:
            # Find the entry to be evicted.
            victim_idx = plru.get_victim()
            victim_line = cache_set[victim_idx]
            # If we are in write-back mode and the cache line is dirty,
            # it has to be written to the lower level of the memory hierarchy
            # before being overwritten.
            if victim_line.valid and victim_line.dirty and self.write_back:
                victim_addr = ((victim_line.tag * self.num_sets) + index) * self.line_size
                if self.lower:
                    self.lower.write(victim_addr)
                elif self.memory: # If L2, send to interconnect
                    self.memory.request(MemoryRequest(self.core_id, self.memory.cycle, 'write', victim_addr))

            # The entry is now valid
            victim_line.valid = True
            victim_line.tag = tag

            # It is dirty (only necessary if write back is active)
            victim_line.dirty = self.write_back
            plru.update_on_access(victim_idx)

        else:
            # If write allocate is false, the data is written to the next level
            # of the memory hierarchy.
            if self.lower:
                self.lower.write(addr)
            elif self.memory: # If L2, send to interconnect
                self.memory.request(MemoryRequest(self.core_id, self.memory.cycle, 'write', addr))



    def stats(self):
        total = self.hits + self.misses
        return {
            "level": self.level,
            "hits": self.hits,
            "misses": self.misses,
            "miss_rate": self.misses / total if total else 0
        }

# ---------------------------------------------------------
# Multi-level cache hierarchy for a core
# Currently supports 2 levels (L1 + shared L2)
# ---------------------------------------------------------
class MultiLevelCache:
    def __init__(self, core_id, l1_conf, shared_cache):
        self.core_id = core_id
        # Create the memory hierarchy
        self.l1 = CacheLevel("L1", core_id, **l1_conf)
        self.l1.lower = shared_cache  # L1 connects to the shared L2 cache

    # Read operation (starts at L1 level)
    def read(self, addr, callback):
        self.l1.read(addr, callback)

    # Write operation (starts at L1 level)
    def write(self, addr):
        self.l1.write(addr)

    def stats(self):
        return {
            "core": self.core_id,
            "L1": self.l1.stats(),
            "L2": self.l1.lower.stats() if self.l1.lower else {} # Shared L2 stats
        }

# ---------------------------------------------------------
# Simple CPU core model that generates memory accesses
# ---------------------------------------------------------
class Core:
    def __init__(self, core_id, cache):
        self.core_id = core_id
        self.cache = cache
        self.cache.core_id = core_id
        self.pending_accesses = []  # List of (op, addr) tuples for pending accesses
        self.stall_op = None        # (op, addr) of the stalled operation, if any
        self.inst = {}            # Instructions scheduled by cycle {cycle: (op, addr)}

    # Load a sequence of instructions
    # Instructions are a dict {cycle: (op, addr)}
    def load_instr(self, inst):
        self.inst=inst

    def read(self, addr, callback):
        self.cache.read(addr, callback)

    def write(self, addr):
        self.cache.write(addr)   


    def enqueue_access(self, op, addr):
        # Enqueue a memory access operation (read or write) in FIFO order 
        # This is used to track pending accesses for dependency checking.
        print(f"{global_cycle}: [Core {self.core_id}] enqueueing access {op.upper()}@{addr} :", end=" ")  
        self.pending_accesses.append((op, addr))
        if len(self.pending_accesses) > 10:
            print(f"{global_cycle}: [Core {self.core_id}] :more than 10 pending accesses!")   
        print(f"{self.pending_accesses}")

    def dequeue_access(self, op, addr):
        # Remove the oldest entry in the queue matching the operation and address
        print(f"{global_cycle}: [Core {self.core_id}] dequeueing access {op.upper()}@{addr} :", end=" ")
        for i, (o, a) in enumerate(self.pending_accesses):
            if o == op and a == addr:
                self.pending_accesses.pop(i)
                print(f"{self.pending_accesses}")                
                return  

    def dependency(self, op, addr):
        # Check if there is a RaW, WaR or WaW dependency on the given address
        # Currenty, we consider all pending accesses in the queue.
        # In this model, we stall as soon as the same address identical, except in RaR
        for (o, a) in self.pending_accesses:
            if (op != o) and (a == addr):
                return True 
        return False

    def tick(self):
        global global_cycle

        # If the core is waiting for a previous access to complete, it cannot issue a new request
        # We consider all dependencies between instruction in RAW, WAR and WAW on addresses.
        if self.stall_op:
            op, addr = self.stall_op
            if not self.dependency(op, addr):
                print(f"{global_cycle}: [Core {self.core_id}] Resuming stalled {op.upper()}@{addr}")
                if op == 'write':
                    self.write(addr)
                    self.stall_op = None
                elif op == 'read':
                    self.enqueue_access('read', addr)
                    self.read(addr, lambda addr=addr: self.dequeue_access('read', addr) )
                    self.stall_op = None
            else:
                print(f"{global_cycle}: [Core {self.core_id}] Still stalled on {op.upper()}@{addr} due to dependency")
                return
            return

        # Check if there is an instruction to execute
        if global_cycle in self.inst:
            op,addr = self.inst[global_cycle]
            if op=='write':
                if self.dependency('write', addr):
                    # There is a pending access with dependency, we stall
                    print(f"{global_cycle}: [Core {self.core_id}] WRITE@{addr} stalled due to dependency")
                    self.stall_op = ('write', addr)
                    return
                else:
                    print(f"{global_cycle}: [Core {self.core_id}] WRITE op at @{addr}")
                    self.write(addr)             
            else:
                if self.dependency('read', addr):
                    # There is a pending access with dependency, we stall
                    print(f"{global_cycle}: [Core {self.core_id}] READ@{addr} stalled due to dependency")
                    self.stall_op = ('read', addr)
                    return
                else:
                    print(f"{global_cycle}: [Core {self.core_id}] READ op at @{addr}")
                    self.enqueue_access('read', addr)
                    self.read(addr, lambda addr=addr: self.dequeue_access('read', addr) )
        else:
             # IDLE cycle, do nothing.
            print(f"{global_cycle}: [Core {self.core_id}] IDLE cycle")

# Simulation setup
random.seed(0) # For reproducible results




class Experiment:
    def __init__(self):
        # Instantiate the DDR Memory
        self.ddr_memory_physical = DDRMemory(num_banks=4)

        # Instantiate the DDR Memory Controller, connected to the physical DDR
        self.ddr_controller = DDRMemoryController(
            self.ddr_memory_physical, 
            tRCD=15,    # Row to Column Delay
            tRP=15,     # Row Precharge
            tCAS=15,    # Column Access Strobe latency
            tRC=30,     # Row Cycle time
            tWR=15,     # Write Recovery Time
            tRTP=8,     # Read to Precharge Time
            tCCD=4)     # Column to Column Delay

        # Create interconnect, connected to the DDR Memory Controller
        self.interconnect = Interconnect(self.ddr_controller, delay=5, bandwidth=4)

        # Create cache configurations
        l1_conf = {'size': 32, 'line_size': 4, 'assoc': 2}
        l2_conf = {'size': 1024, 'line_size': 4, 'assoc': 16}

        # Create shared L2 Cache, connected to the Interconnect
        shared_l2 = CacheLevel("L2", core_id="anycore", memory=self.interconnect, **l2_conf)

        # Create Core-specific Multi-Level Caches, connected to the shared L2
        self.mem_core0 = MultiLevelCache(0, l1_conf, shared_l2)
        self.mem_core1 = MultiLevelCache(1, l1_conf, shared_l2)

        # Create cores
        self.core0 = Core(0, self.mem_core0)
        self.core1 = Core(1, self.mem_core1)   

    def load_instr(self, core0_inst, core1_inst):
        self.core0.load_instr(core0_inst)
        self.core1.load_instr(core1_inst)

    def simulate(self, cycles):
        global global_cycle
        global_cycle = 0
        for cycle in range(cycles):
            # /!\ All components tick at the same frequency
            self.core0.tick()
            self.core1.tick()
            self.interconnect.tick()
            self.ddr_controller.tick()
            self.ddr_memory_physical.tick()
            # Update global clock (shared variable)
            global_cycle+=1    
        # Report results
        print("\n--- Simulation Stats ---")
        print(self.mem_core0.stats())
        print(self.mem_core1.stats())



print("===================================================================")
print("1 core, 2 read cycles, same cache line, no dependency")
print("===================================================================")
# 1st RD => cache miss => DDR reads transaction 
# 2nd RD => cache hit => no DDR transaction
# Create instruction sequences
inst0 = { 0: ('read', 0), 60: ('read', 2) }
inst1 = {  }

exp=Experiment()
exp.load_instr(inst0, inst1)

exp.simulate(100)

print("===================================================================")
print("1 core, 2 read cycles, same bank, different rows, no dependency")
print("===================================================================")
# 1st RD => cache miss => DDR reads transaction 
# 2nd RD => cache hit => no DDR transaction
# Create instruction sequences
inst0 = { 0: ('read', 0), 60: ('read', 2000) }
inst1 = {  }

exp=Experiment()
exp.load_instr(inst0, inst1)

exp.simulate(100)

print("===================================================================")
print("1 core, 2 read cycles, different bank, no dependency")
print("===================================================================")
# bank nb = addr % num_banks, with num_banks=4
# row nb = addr // 16
# 1st RD in bank 0, row 0 
# 1st RD in bank 1, row 1
# 2nd RD => cache hit => no DDR transaction
# Create instruction sequences
inst0 = { 0: ('read', 0), 60: ('read', 17) }
inst1 = {  }

exp=Experiment()
exp.load_instr(inst0, inst1)

exp.simulate(100)

print("===================================================================")
print("2 cores, arbitrary sequence")
print("===================================================================")
# Create instruction sequences
inst0 = { 0: ('read', 0), 
         10: ('write',5), 
         60: ('read', 17) }
inst1 = { 3: ('read', 2), 
         15: ('write',6), 
         45: ('read', 23) }

exp=Experiment()
exp.load_instr(inst0, inst1)

exp.simulate(200)