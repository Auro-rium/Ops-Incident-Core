# Sample Output · [Docs Hub](./README.md)

## Query

Why did latency increase after the last deploy?

## Expected behavior

- best matching evidence is returned even when evidence is weak
- structured investigation response includes:
  - task type
  - entities
  - timeline
  - hypotheses
  - likely root cause
  - unknowns / missing data

If evidence is weak, the root-cause field should say:

`Evidence is insufficient to identify a confident root cause.`
