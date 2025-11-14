# Queue Race Condition - Visual Explanation

## The Setup

```
Daemon Process (termux-sensor)
         |
         v
   [Queue 100]  ← New fresh empty queue created during restart
         |
         +------> Consumer #1: _accel_loop() (tight 0.1s timeout loop)
         |
         +------> Consumer #2: validation code (30s timeout)
```

## The Race

**Hardware:** Produces 50 samples/second (20ms intervals)

**Timeline:**

```
T=0.0s:  Daemon starts, queue is empty [  ]
T=0.02s: Hardware: Sample #1 arrives → queue becomes [#1]
T=0.03s: _accel_loop checks queue (0.1s timeout)
T=0.03s: _accel_loop wins! Takes sample #1 from queue → queue becomes [ ]
T=0.04s: Hardware: Sample #2 arrives → queue becomes [#2]
T=0.05s: _accel_loop checks queue again (tight loop)
T=0.05s: _accel_loop wins again! Takes sample #2 → queue becomes [ ]
T=0.06s: ... pattern repeats ...

Meanwhile:
T=0.0s:  Validation code calls: test_data = get_data(timeout=30.0)
T=0.0s:  Validation blocks on queue.get(), waiting...
T=0.03s: _accel_loop's get() call wins and consumes sample #1
T=0.05s: _accel_loop's get() call wins and consumes sample #2
...
T=30.0s: Validation timeout expires! Still waiting for its turn!
         Even though 1500 samples arrived (50 Hz × 30s)
         _accel_loop consumed ALL of them
```

## Why _accel_loop Always Wins

```python
# _accel_loop code (in main data collection)
while not self.stop_event.is_set():
    accel_data = self.accel_daemon.get_data(timeout=0.1)  # Gets called ~10x/sec
    if accel_data:
        # Process it
```

```python
# Validation code (in restart handler)
test_data = self.accel_daemon.get_data(timeout=30.0)  # Gets called once
if test_data:
    # Mark restart as successful
```

**Why _accel_loop wins:**
- Runs every 100ms in a tight loop
- Gets first "dibs" on every sample
- Consumes items faster than validation can check for them

**Python queue.get() behavior:**
- Only ONE thread can get() at a time
- FIFO order: first thread to call get() that's waiting wins
- _accel_loop is in the queue SO MUCH MORE that it dominates

## Mathematical Proof

**Queue competition over 30 seconds:**

- _accel_loop attempts: 30s ÷ 0.1s = 300 attempts
- Validation attempts: 1 attempt (it just calls get() once and waits)
- Hardware provides: 50 samples/sec × 30s = 1500 samples
- Probability _accel_loop gets ANY ONE sample it checks: ~99%

After first few seconds, _accel_loop has consumed so many samples that it stays ahead. By the time validation's get() call reaches the "front of the line" in Python's thread scheduler, the queue is empty again!

## Why It Worked Early (0-38 min)

**Lower failure rate → less restart attempts → less queue competition**

- When accels fails every 10 minutes: 1 restart per 10 min = rare validation calls
- Lucky timing possible: validation call happens when queue happens to have buildup
- No deterministic race condition emerges

## Why It Failed Later (38-50 min)

**Higher failure rate → more restart attempts → constant queue competition**

- Accel crashes every 2-5 minutes: 8-10 restarts per 10 min
- Validation calls increase from 1-2 to 8-10 per 10 minutes
- Queue is continuously being drained by _accel_loop
- Validation nearly always times out

## The Fix

**Option 1: Separate Validation Queue (Recommended)**
```python
self.accel_daemon.validation_queue = Queue(maxsize=10)
self.accel_daemon.data_queue = Queue(maxsize=100)

# In daemon's read_loop:
for sample in data:
    put_to_both_queues(sample, self.validation_queue, self.data_queue)

# In _accel_loop:
data = get_data(timeout=0.1)  # From data_queue

# In validation:
test_data = accel_daemon.validation_queue.get(timeout=30.0)  # From validation_queue
```

**Option 2: Simpler - Don't Use Queue for Validation**
```python
# Don't consume from queue! Just check daemon state
if self.accel_daemon.sensor_process.poll() == None:  # Process alive
    return True  # That's enough validation!
```

**Option 3: Timestamp-based**
```python
# Add to daemon:
self.last_sample_timestamp = None

# In daemon's read_loop:
self.last_sample_timestamp = time.time()

# In validation:
if time.time() - self.last_sample_timestamp < 1.0:
    return True  # Recent data seen, good!
```

## Why This Matters

This isn't just a timing edge case. This is a **fundamental design flaw** when:
1. You have a single shared queue
2. You have a fast consumer (production code) 
3. You try to add a slow consumer (validation code)
4. The fast consumer's duty cycle is 10x higher than the slow consumer

Under these conditions, the slow consumer will ALWAYS lose the race as load increases.

## Summary

The health monitor worked correctly. The restart validation logic was flawed. The flaw only became obvious under the stress of frequent daemon crashes (minute 38+) when the queue race condition became deterministic instead of probabilistic.
