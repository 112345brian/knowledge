"""What a build step is given besides its database connection: where the data lives and how to read it.

Model ring: a value, no I/O. `inputs` satisfies `inputs_port.Inputs` (the real one is `local_inputs`) and `stores` satisfies
`step_stores_port.StepStores` (`local_stores`); they are typed `Any` here only because the ports live in an outer ring.
"""
from dataclasses import dataclass
from typing import Any

from locations import Locations


@dataclass(frozen=True)
class StepContext:
    locations: Locations
    inputs: Any
    stores: Any
