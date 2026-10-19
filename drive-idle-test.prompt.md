---
description: "Run the drive spindown idle test to verify a service is not preventing rotational drives from sleeping"
agent: "agent"
---

# Drive Spindown Idle Test

Perform the following test to verify that a running service does not prevent rotational drives from spinning down. This is the correct methodology — the test checks whether drives **spin down after being spun up**, not whether the service spins them up in the first place.

## Prerequisites

- The service under test must already be running.
- Identify the target rotational drive (e.g. `/dev/sda`).

## Steps

> Note: check `/etc/proxmox-mqtt/config.yaml` for poll rate. Make sure the spindown timer is longer than the poll rate. The following example assumes a 30 second poll rate.

1. **Set a short spindown timer** (60 seconds):
   ```
   hdparm -S 12 /dev/sda
   ```
   (`-S 12` = 12 × 5s = 60s idle timeout)

2. **Force the drive to spin up** by reading from it:
   ```
   dd if=/dev/sda of=/dev/null bs=4k count=1 2>/dev/null
   ```

3. **Confirm the drive is active**:
   ```
   hdparm -C /dev/sda
   ```
   Expected: `drive state is: active/idle` (or `unknown`, which also means spinning)

4. **Wait 90 seconds** (longer than the 60s idle timeout):
   ```
   sleep 90
   ```

5. **Check the drive state**:
   ```
   hdparm -C /dev/sda
   ```

## Interpreting Results

- **`standby`** — the drive spun down. The service is NOT keeping it awake.
- **`active/idle`** or **`unknown`** — the drive did NOT spin down. Something is touching it every polling interval.

## After Testing

Restore the normal spindown timer (e.g. 20 minutes):
```
hdparm -S 240 /dev/sda
```
(`-S 240` = 20 minutes)

## One-liner

Run this as a single command to automate the full test:
```
hdparm -S 12 /dev/sda && dd if=/dev/sda of=/dev/null bs=4k count=1 2>/dev/null && echo "SPUN UP:" && hdparm -C /dev/sda && echo "Waiting 90s..." && sleep 90 && echo "=== RESULT ===" && hdparm -C /dev/sda && hdparm -S 240 /dev/sda
```
