"""Behavioural contracts every adapter of a port must satisfy.

A port with two implementations is only an abstraction if both behave the same
way. Without a shared suite the implementations drift and the port becomes a lie
told in two dialects, so each contract here is a base class an adapter's own test
module subclasses, supplying the implementation through one fixture.

These assert *behaviour*, not *quality*. That a real model retrieves better than
a fake one is a question for the evaluation set, not for a contract: a contract
that encodes quality thresholds fails for reasons that have nothing to do with
the interface being honoured.
"""
