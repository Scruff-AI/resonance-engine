# Golden-Weave Memory System for Khra'gixx Lattice Observer
# Version 1.0 - API Extensions and Hysteresis Implementation
# Author: CTO Agent
# Date: 2026-03-22

"""
This module extends the lattice_observer.py with:
1. Local property queries (density, stress, vorticity at specific coordinates)
2. Attractor storage and recall system
3. Hysteresis buffer for stress tensor memory
4. Persistent attractor library in JSON format
"""

import json
import os
import numpy as np
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, asdict
from collections import deque

# Golden ratio constants
PHI = (1 + np.sqrt(5)) / 2  # 1.6180339887...
PHI_SQUARED = PHI ** 2       # 2.618...
INV_PHI_SQUARED = 1 / PHI_SQUARED  # ~0.382 (decay factor)

@dataclass
class LocalFieldState:
    """Represents the field state at a specific location."""
    x: int
    y: int
    density: float
    stress_xx: float
    stress_yy: float
    stress_xy: float
    vorticity: float
    velocity_x: float
    velocity_y: float
    timestamp: str
    cycle: int
    
    @property
    def stress_divergence(self) -> float:
        """Compute stress divergence (charge analog)."""
        # Approximate divergence from stress components
        return self.stress_xx + self.stress_yy
    
    @property
    def stress_magnitude(self) -> float:
        """Compute total stress magnitude."""
        return np.sqrt(self.stress_xx**2 + self.stress_yy**2 + 2*self.stress_xy**2)


@dataclass
class AttractorDefinition:
    """Defines a stored attractor with its properties."""
    name: str
    center_x: int
    center_y: int
    radius: int
    creation_time: str
    cycle_created: int
    
    # Field properties at center
    center_density: float
    center_stress_div: float
    center_vorticity: float
    center_coherence: float
    
    # Injection parameters used to create it
    injection_amplitude: float
    injection_radius: int
    num_injections: int
    omega_at_creation: float
    
    # Full field snapshot (optional, for precise recall)
    density_snapshot: Optional[List[float]] = None
    
    @property
    def atomic_number_analog(self) -> int:
        """Derive atomic number analog from vorticity."""
        # Map vorticity to Z: low |ω| → low Z, high |ω| → high Z
        return int(self.center_vorticity * 1000)
    
    @property
    def charge_analog(self) -> str:
        """Derive charge from stress divergence sign."""
        if self.center_stress_div < -0.0001:
            return "negative"
        elif self.center_stress_div > 0.0001:
            return "positive"
        else:
            return "neutral"


class HysteresisBuffer:
    """
    Sliding window buffer for stress tensor history.
    Provides memory of past states that influences current dynamics.
    """
    
    def __init__(self, window_size: int = 15, decay_factor: float = INV_PHI_SQUARED):
        self.window_size = window_size
        self.decay_factor = decay_factor
        
        # Circular buffers for stress components
        self.stress_xx_buffer = deque(maxlen=window_size)
        self.stress_yy_buffer = deque(maxlen=window_size)
        self.stress_xy_buffer = deque(maxlen=window_size)
        
        # Weighted moving average
        self.current_weight = 1.0
        
    def update(self, stress_xx: float, stress_yy: float, stress_xy: float):
        """Add new stress tensor to buffer."""
        self.stress_xx_buffer.append(stress_xx)
        self.stress_yy_buffer.append(stress_yy)
        self.stress_xy_buffer.append(stress_xy)
    
    def get_effective_stress(self) -> Tuple[float, float, float]:
        """
        Compute effective stress with phi-decay weighting.
        Recent stresses have higher weight, older stresses decay by φ⁻².
        """
        if not self.stress_xx_buffer:
            return 0.0, 0.0, 0.0
        
        # Apply decay weights: most recent = 1, older = φ⁻², φ⁻⁴, ...
        weights = [self.decay_factor ** i for i in range(len(self.stress_xx_buffer))]
        weights = weights[::-1]  # Reverse so most recent has highest weight
        weight_sum = sum(weights)
        
        # Weighted averages
        eff_xx = sum(w * s for w, s in zip(weights, self.stress_xx_buffer)) / weight_sum
        eff_yy = sum(w * s for w, s in zip(weights, self.stress_yy_buffer)) / weight_sum
        eff_xy = sum(w * s for w, s in zip(weights, self.stress_xy_buffer)) / weight_sum
        
        return eff_xx, eff_yy, eff_xy
    
    def compute_omega_modulation(self, base_omega: float) -> float:
        """
        Modulate omega based on hysteresis stress magnitude.
        High accumulated stress → higher effective viscosity.
        """
        eff_xx, eff_yy, eff_xy = self.get_effective_stress()
        stress_mag = np.sqrt(eff_xx**2 + eff_yy**2 + 2*eff_xy**2)
        
        # Modulate: base + stress-dependent term (bounded)
        modulation = 0.1 * stress_mag * PHI  # Golden-scaled modulation
        return min(base_omega + modulation, 2.15)  # Cap at 2.15


class GoldenWeaveMemorySystem:
    """
    Main memory system integrating attractor storage and hysteresis.
    """
    
    def __init__(self, attractor_dir: str = "attractors", grid_size: int = 1024):
        self.attractor_dir = Path(attractor_dir)
        self.attractor_dir.mkdir(exist_ok=True)
        self.grid_size = grid_size
        
        # Initialize hysteresis buffer
        self.hysteresis = HysteresisBuffer(window_size=15)
        
        # Cache of loaded attractors
        self.attractor_cache: Dict[str, AttractorDefinition] = {}
        
        # Load existing attractors
        self._load_attractors()
    
    def _load_attractors(self):
        """Load all stored attractors from disk."""
        for attractor_file in self.attractor_dir.glob("*.json"):
            with open(attractor_file, 'r') as f:
                data = json.load(f)
                attractor = AttractorDefinition(**data)
                self.attractor_cache[attractor.name] = attractor
    
    def query_local_field(self, x: int, y: int, 
                          density_field: np.ndarray,
                          stress_xx: np.ndarray,
                          stress_yy: np.ndarray,
                          stress_xy: np.ndarray,
                          vorticity_field: np.ndarray,
                          velocity_field: np.ndarray,
                          current_cycle: int) -> LocalFieldState:
        """
        Query the field state at a specific (x, y) coordinate.
        
        Args:
            x, y: Grid coordinates (0 to grid_size-1)
            Various field arrays from the lattice daemon
            current_cycle: Current simulation cycle
            
        Returns:
            LocalFieldState with all properties at that location
        """
        # Bounds check
        x = max(0, min(x, self.grid_size - 1))
        y = max(0, min(y, self.grid_size - 1))
        
        return LocalFieldState(
            x=x,
            y=y,
            density=float(density_field[y, x]),
            stress_xx=float(stress_xx[y, x]),
            stress_yy=float(stress_yy[y, x]),
            stress_xy=float(stress_xy[y, x]),
            vorticity=float(vorticity_field[y, x]),
            velocity_x=float(velocity_field[y, x, 0]),
            velocity_y=float(velocity_field[y, x, 1]),
            timestamp=datetime.now().isoformat(),
            cycle=current_cycle
        )
    
    def store_attractor(self, name: str, center_x: int, center_y: int, radius: int,
                        local_state: LocalFieldState,
                        injection_params: Dict,
                        density_snapshot: Optional[np.ndarray] = None) -> AttractorDefinition:
        """
        Store a new attractor definition.
        
        Args:
            name: Unique identifier for this attractor
            center_x, center_y: Center coordinates
            radius: Radius of the attractor region
            local_state: LocalFieldState at center
            injection_params: Dict with 'amplitude', 'radius', 'num_injections', 'omega'
            density_snapshot: Optional full density field snapshot
            
        Returns:
            Stored AttractorDefinition
        """
        attractor = AttractorDefinition(
            name=name,
            center_x=center_x,
            center_y=center_y,
            radius=radius,
            creation_time=datetime.now().isoformat(),
            cycle_created=local_state.cycle,
            center_density=local_state.density,
            center_stress_div=local_state.stress_divergence,
            center_vorticity=local_state.vorticity,
            center_coherence=0.0,  # To be filled from global state
            injection_amplitude=injection_params.get('amplitude', 0.05),
            injection_radius=injection_params.get('radius', 20),
            num_injections=injection_params.get('num_injections', 5),
            omega_at_creation=injection_params.get('omega', 1.97),
            density_snapshot=density_snapshot.flatten().tolist() if density_snapshot is not None else None
        )
        
        # Save to disk
        attractor_file = self.attractor_dir / f"{name}.json"
        with open(attractor_file, 'w') as f:
            json.dump(asdict(attractor), f, indent=2)
        
        # Cache
        self.attractor_cache[name] = attractor
        
        return attractor
    
    def recall_attractor(self, name: str) -> Optional[AttractorDefinition]:
        """
        Retrieve an attractor definition for reinjection.
        
        Args:
            name: Attractor identifier
            
        Returns:
            AttractorDefinition or None if not found
        """
        return self.attractor_cache.get(name)
    
    def list_attractors(self) -> List[str]:
        """Return list of all stored attractor names."""
        return list(self.attractor_cache.keys())
    
    def get_attractor_properties(self, name: str) -> Optional[Dict]:
        """Get human-readable properties of an attractor."""
        attractor = self.recall_attractor(name)
        if attractor is None:
            return None
        
        return {
            "name": attractor.name,
            "location": f"({attractor.center_x}, {attractor.center_y})",
            "atomic_number_analog": attractor.atomic_number_analog,
            "charge_analog": attractor.charge_analog,
            "density": attractor.center_density,
            "stress_divergence": attractor.center_stress_div,
            "vorticity": attractor.center_vorticity,
            "created": attractor.creation_time,
            "injections": attractor.num_injections
        }
    
    def update_hysteresis(self, stress_xx: float, stress_yy: float, stress_xy: float):
        """Update the hysteresis buffer with current stress state."""
        self.hysteresis.update(stress_xx, stress_yy, stress_xy)
    
    def get_effective_omega(self, base_omega: float) -> float:
        """Get omega modulated by hysteresis memory."""
        return self.hysteresis.compute_omega_modulation(base_omega)


# Integration with lattice_observer.py
# Add these methods to the LatticeObserver class:

class LatticeObserverExtensions:
    """
    Mixin class to extend LatticeObserver with Golden-Weave memory system.
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.memory_system = GoldenWeaveMemorySystem()
    
    def handle_query_local(self, x: int, y: int) -> Dict:
        """Handle CMD: query_local x y"""
        # Access current field state from daemon telemetry
        local_state = self.memory_system.query_local_field(
            x=x, y=y,
            density_field=self.current_density,
            stress_xx=self.current_stress_xx,
            stress_yy=self.current_stress_yy,
            stress_xy=self.current_stress_xy,
            vorticity_field=self.current_vorticity,
            velocity_field=self.current_velocity,
            current_cycle=self.cycle
        )
        
        return {
            "command": "query_local",
            "x": x,
            "y": y,
            "density": local_state.density,
            "stress_divergence": local_state.stress_divergence,
            "stress_magnitude": local_state.stress_magnitude,
            "vorticity": local_state.vorticity,
            "velocity": [local_state.velocity_x, local_state.velocity_y],
            "cycle": local_state.cycle
        }
    
    def handle_store_attractor(self, name: str, x: int, y: int, radius: int) -> Dict:
        """Handle CMD: store_attractor name x y radius"""
        # Query current state at location
        local_state = self.memory_system.query_local_field(
            x=x, y=y,
            density_field=self.current_density,
            stress_xx=self.current_stress_xx,
            stress_yy=self.current_stress_yy,
            stress_xy=self.current_stress_xy,
            vorticity_field=self.current_vorticity,
            velocity_field=self.current_velocity,
            current_cycle=self.cycle
        )
        
        # Get injection params from recent history (simplified)
        injection_params = {
            'amplitude': self.last_injection_amplitude if hasattr(self, 'last_injection_amplitude') else 0.05,
            'radius': self.last_injection_radius if hasattr(self, 'last_injection_radius') else 20,
            'num_injections': self.last_num_injections if hasattr(self, 'last_num_injections') else 5,
            'omega': self.current_omega
        }
        
        attractor = self.memory_system.store_attractor(
            name=name,
            center_x=x,
            center_y=y,
            radius=radius,
            local_state=local_state,
            injection_params=injection_params,
            density_snapshot=self.current_density if radius > 50 else None
        )
        
        return {
            "command": "store_attractor",
            "name": name,
            "properties": self.memory_system.get_attractor_properties(name),
            "status": "stored"
        }
    
    def handle_recall_attractor(self, name: str) -> Dict:
        """Handle CMD: recall_attractor name"""
        attractor = self.memory_system.recall_attractor(name)
        if attractor is None:
            return {"command": "recall_attractor", "name": name, "error": "not found"}
        
        # Return parameters for reinjection
        return {
            "command": "recall_attractor",
            "name": name,
            "center": [attractor.center_x, attractor.center_y],
            "injection_amplitude": attractor.injection_amplitude,
            "injection_radius": attractor.injection_radius,
            "num_injections": attractor.num_injections,
            "omega": attractor.omega_at_creation,
            "status": "ready_for_injection"
        }
    
    def handle_list_attractors(self) -> Dict:
        """Handle CMD: list_attractors"""
        attractors = self.memory_system.list_attractors()
        properties = [self.memory_system.get_attractor_properties(name) for name in attractors]
        
        return {
            "command": "list_attractors",
            "count": len(attractors),
            "attractors": properties
        }


# Example usage script (for testing):
"""
# Test the memory system

from golden_weave_memory import GoldenWeaveMemorySystem, LocalFieldState

# Initialize
memory = GoldenWeaveMemorySystem(attractor_dir="attractors", grid_size=1024)

# Simulate querying local field (would use actual daemon data)
local_state = LocalFieldState(
    x=512, y=512,
    density=0.984,
    stress_xx=-0.0005,
    stress_yy=0.0003,
    stress_xy=-0.0001,
    vorticity=0.021,
    velocity_x=0.1, velocity_y=0.05,
    timestamp="2026-03-22T12:00:00",
    cycle=100000
)

# Store an attractor
attractor = memory.store_attractor(
    name="proton_analog",
    center_x=512, center_y=512, radius=20,
    local_state=local_state,
    injection_params={'amplitude': 0.05, 'radius': 20, 'num_injections': 5, 'omega': 1.97}
)

print(f"Stored attractor: {attractor.name}")
print(f"Z analog: {attractor.atomic_number_analog}")
print(f"Charge: {attractor.charge_analog}")

# List all attractors
print(f"All attractors: {memory.list_attractors()}")

# Recall
recalled = memory.recall_attractor("proton_analog")
print(f"Recalled: {recalled}")
"""

# End of golden_weave_memory.py
