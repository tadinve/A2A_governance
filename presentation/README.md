# presentation

Materials for the live demonstration of this project.

## Contents

| File | What it is |
|---|---|
| `deck_v3_4.md` | The current deck, as text. 16 slides, "Agent Identity and Delegated Procurement" |

The binary (`A2A_Governance_Demo_Deck_v3_4.pptx`) is **not** in here -- drop it
alongside this file if you want them together. The text version is kept because
a deck that makes specific factual claims about a live system needs to be
reviewable and diffable against the code, which a .pptx is not.

## What the deck claims, and what currently backs it

Every slide was checked against the live deployment. The claims that depend on
something runtime-verifiable:

| Slide | Claim | Verified by |
|---|---|---|
| 8 | five denial checks pass | `python3 cloud/verify_cloud.py` |
| 11 | InvRead / ProcureWrite tool surfaces | live `tools/list` against each connector |
| 12 | UI spans join one trace | `src/inventory_ui/telemetry.py` |
| 13 | three Agent Identities | `cloud/registry_principals.py --show` |
| 14 | UI invokes Inventory Agent | `EXECUTION_MODE=agent` on the Cloud Run service |

## Before presenting

```bash
source activate.sh
python3 cloud/verify_cloud.py          # slide 8 claims this passes 5/5
```

**Slide 8 says "Re-run it immediately before presenting."** Take that
literally: it is the slide most likely to be wrong, because it asserts a live
result rather than a design property.

Two other pre-flight items the deck depends on:

- an unrelated **Movie Pitch** engine exists in the project and appears in the
  identity listing on slide 13 step 1 -- delete it or have a sentence ready;
- the Cloud Run UI must still be on `EXECUTION_MODE=agent` for slides 13 and 14.

## Agent Gateway is deliberately absent from this deck

A real Google-managed Agent-to-Anywhere gateway now exists in the project, with
the Agent Registry attached, eight registered destinations and IAP in
`DRY_RUN`. None of it appears in these slides, and that is correct: the work
postdates the deck and enforcement has never been demonstrated.

If it comes up, the accurate position is that the gateway is provisioned and
binding has been exercised, that a bound agent was observed returning a live
model response through it, and that enforcement is not claimed. There is also a
measured limitation worth stating plainly: a TLS-inspecting egress gateway
breaks gRPC clients unless its CA is trusted before the process starts, because
gRPC's C core caches its root store at first channel creation and reads none of
the Python TLS environment variables. See `cloud/DEPLOYED_AGENTS.md`.

Do not say the gateway blocks anything. It does not, yet.
