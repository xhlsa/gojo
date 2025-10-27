# Accelerometer Reader: Python vs Bash Analysis

## Summary
**Winner: Bash script (`accel_reader.sh`)**

The bash implementation works reliably while the Python version struggles with process management.

---

## The Problem: Long-Running Daemon

`termux-sensor` is designed as a **daemon** that continuously streams sensor data:

```
{sensor data 1}
{sensor data 2}
{sensor data 3}
... (never terminates naturally)
```

It doesn't follow the typical Unix pattern of:
- Accept input
- Process
- Exit cleanly

This created fundamental issues with Python's subprocess handling.

---

## Python Attempts & Why They Failed

### Attempt 1: `subprocess.run()` with timeout
```python
result = subprocess.run(
    ["termux-sensor", "-s", "ACCELEROMETER"],
    capture_output=True,
    text=True,
    timeout=1
)
```

**Problem:**
- Process never terminates (it's a daemon)
- Timeout fires and raises `TimeoutExpired`
- The exception's `stdout`/`stderr` attributes may be empty or incomplete
- Output was unreliable

### Attempt 2: `subprocess.Popen()` with explicit kill
```python
proc = subprocess.Popen([...], stdout=PIPE, stderr=PIPE)
try:
    stdout, stderr = proc.communicate(timeout=1)
except subprocess.TimeoutExpired:
    proc.kill()
    stdout, stderr = proc.communicate()
```

**Problem:**
- `proc.communicate()` blocks until process exits
- Had to catch timeout, kill process, then get output
- Output handling was complex and error-prone
- No clean way to grab "first N lines" before killing

---

## Why Bash Works Better

### Bash Solution
```bash
{
    sleep 0.2
    pkill -f termux-sensor 2>/dev/null || true
} &
termux-sensor -s ACCELEROMETER 2>&1 | head -20
```

**Why this works:**

1. **Process Group Management**: Bash spawns the pkill in background (`&`), so it doesn't block
2. **Pipe Closure**: `head -20` naturally closes the pipe after reading 20 lines
3. **Natural Kill**: The sensor process dies when pipe closes (SIGPIPE)
4. **Backup Kill**: If pipe doesn't close it fast enough, `pkill` terminates it after 0.2s
5. **Dual Exit Path**: Either way, the process terminates cleanly

**Key insight:** Using pipes and process redirection is more natural for Unix daemons than trying to manage subprocesses programmatically.

---

## Subprocess Behavior Differences

| Aspect | Python | Bash |
|--------|--------|------|
| **Timeout handling** | Raises exception, messy output capture | Built into command execution |
| **Process groups** | Single process focus | Can manage background jobs |
| **Pipe semantics** | Harder to leverage SIGPIPE | Native to shell philosophy |
| **Error cleanup** | Must kill explicitly | Automatic with job control |
| **Code clarity** | Complex try/except blocks | Simple one-liner |

---

## When Python Might Still Work

For a proper Python solution, you'd need:

1. **Non-blocking read loop:**
   ```python
   import select
   while True:
       ready = select.select([proc.stdout], [], [], 0.3)
       if ready[0]:
           line = proc.stdout.readline()
           # process line
       else:
           # timeout - kill and exit
           proc.kill()
           break
   ```

2. **Or use threading** to manage the timeout separately from reading

3. **Complexity:** Much more code, more edge cases, harder to debug

---

## Lessons Learned

**When to use bash vs Python for subprocess management:**

| Use Bash | Use Python |
|----------|-----------|
| Simple command pipelines | Complex business logic |
| Long-running daemons | One-shot executables |
| Process kill/cleanup | Structured error handling |
| Stream processing | Data transformation |
| Shell job control | Tight integration needed |

**For this accelerometer use case:** The problem is fundamentally about **managing a daemon process**, which is a Unix/shell concern. Bash is the right tool.

---

## Files

- **`accel_reader.sh`** - Working bash implementation ✅
- **`accel_reader_legacy.py`** - Python attempt (documented for reference)
- **`ACCEL_FINDINGS.md`** - This file
