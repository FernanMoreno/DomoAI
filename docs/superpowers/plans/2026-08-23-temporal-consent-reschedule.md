# Plan: Temporal Consent and Safe Rescheduling

1. Prove window/digest drift and DST behavior with red tests.
2. Introduce canonical execution-window evidence and schedule revisions.
3. Make rescheduling invalidate old approval and use pending-row CAS.
4. Protect bundle members and scheduler claims with the same evidence.
5. Verify real SQLite restart/DST composition and architecture gates.

Stop condition: no approved physical action may execute at a time not covered by the approval assertion.
