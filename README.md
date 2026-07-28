# IncidentOps Core

Technical architecture, runtime boundaries, security model, RAG design, and
known limitations are documented in [technical.md](technical.md). The phased
implementation and release gates are in [plan.md](plan.md).

The repository intentionally keeps this README minimal. `technical.md` is the
technical source of truth; `plan.md` distinguishes implemented behavior from
remaining release work. Azure deployment is manual and gated; CI runs checks on
pushes but does not create cloud resources automatically.
