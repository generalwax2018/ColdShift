# ColdShift

Selective process offloading app.

**Goal:** Measure running processes, identify good candidates to move to a fast SSD, and free physical DDR5 for intensive workloads.

## Overview

ColdShift helps reclaim physical RAM by:
1. Measuring memory usage and activity of running processes
2. Classifying them as Protect / Offload / Leave alone
3. Moving low-priority processes to a fast secondary drive (SSD)
4. Keeping high-priority workloads in DDR5

## Status

Early stage — measurement and classification layer first.

## Platform

Starting with Windows. Linux support planned later.

## Next Steps

- Process measurement (working set, idle time, page faults)
- Classification rules
- Offload actions (trim / suspend / page)
- Simple UI or CLI dashboard
