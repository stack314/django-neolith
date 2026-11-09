# 04. Events

**Status:** outline only. Not part of 0.1 conformance. Written here so that the contract and transport design leaves room for it.

- An app publishes a named, versioned event with a payload record from the portable type system.
- Subscribers declare the events they consume. **EVT-1.** A subscriber MUST tolerate at-least-once delivery.
- Publication is transactional with the publisher's own data change, through an **outbox** in the publisher's database. The outbox is what makes publishing reliable in every mode, and what de-duplicates when the same app runs attached and detached at once.
- **EVT-2.** When publisher and subscriber share a host, delivery MAY be in-process after the publisher's transaction commits. When they do not, delivery goes through a broker. The broker is integrated, not provided.
- Events replace in-process signal mechanisms for anything that crosses an app boundary.
