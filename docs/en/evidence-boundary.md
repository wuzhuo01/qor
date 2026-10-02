# Evidence boundary

Evidence Source is governance metadata. It is not proof that a claim is true.

An artifact carries evidence items. Each item has a source class (`kind`), a locator, and a producer. A finding may name a claim class. Review uses those fields to decide whether the declared source is allowed to support that class of claim.

## What QOR checks

QOR checks three things:

- The evidence source class is one of `reasoning`, `external`, `tool`, or `human`. Older stored labels can still be loaded. An unknown class is rejected.
- The claim and the evidence match. A finding states how the claim is classified. Review compares that class with the cited evidence kind.
- Reasoning is not treated as fact. A factual claim that cites `reasoning` fails review. `reasoning` may support a hypothesis, a speculation, or a research direction.

The producing agent writes `kind`. The runtime copies that declaration onto the artifact. It does not replace it with a source the runtime observed.

## What QOR does not check

QOR does not check:

- Whether `external` data is real.
- Whether a `tool` actually ran.
- Whether a locator names a resource that exists in the world.

A non-empty locator is required so a later reader can see where the producer pointed. The locator is not fetched, executed, or compared with a tool record. There is no V1.0 record of tool execution to compare it with.

`external`, `tool`, and `human` can therefore be declared without a corresponding observation. A missing claim class skips the reasoning-as-fact check.

## Metadata, not proof

Evidence Source records how the organization classified the support for a claim. It lets review refuse a fact that is supported only by the producer's own reasoning.

It does not prove that an external statement happened, that a tool returned the cited result, or that the locator can be opened. Passing review means the declared source class is allowed for that claim class. It does not mean the claim is true.

## Not in V1.0

A later version may verify provenance for `external` and `tool` evidence: binding a declared source to an observed fetch or a recorded tool run.

That check is not part of V1.0. V1.0 keeps the source class as declared metadata and applies the claim-class rule above.
