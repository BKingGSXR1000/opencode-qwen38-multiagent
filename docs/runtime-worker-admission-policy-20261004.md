# Runtime implementation-worker admission policy

The scheduler had a proven hardcoded capacity of three implementation workers.
This change preserves three as the default but moves the value behind one
validated runtime policy shared by all supervisor slot calculations.

Environment variable: V2_MAX_CONCURRENT_IMPLEMENTATION_WORKERS.
Accepted values: integer 1 through 8. Missing means 3. Invalid values do not
silently fall back: slot admission fails closed and the scheduler projection
surfaces an error / zero available slots.

This is admission capacity, not a promise that the model backend can use that
many requests efficiently. A deployment profile must benchmark its own GPU,
VRAM, KV-cache and max-sequence constraints before increasing it. The setting
does not route a worker to a GPU; it only provides the central capacity control
needed before multi-backend/GPU routing is introduced.

scheduler_policy.py is included in the long-lived supervisor runtime
fingerprint, so source replacement requires the normal compile-and-reexec path.
