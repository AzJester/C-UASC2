"""Deterministic, explicitly notional models used by the edge simulator.

This module deliberately contains no NATS dependency so its behavior can be
verified without a running broker.  Parameters are illustrative and are not
derived from fielded sensor or effector performance data.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping


NOTIONAL_MODEL_NOTICE = "NOTIONAL REFERENCE MODEL - NOT FIELD PERFORMANCE DATA"
DEFAULT_ASSET_LAT = 32.699
DEFAULT_ASSET_LON = -117.215
DEFAULT_START_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def parse_utc(value: str | datetime) -> datetime:
    """Parse an RFC3339 value and require an explicit UTC offset."""
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def canonical_safety_hash(value: object) -> str:
    """Canonical hash for signed safety structures.

    JSON Schema treats integral `150` and `150.0` as the same number; normalize
    them so independent conformant producers derive the same signed hash.
    """
    def normalize(item: object) -> object:
        if isinstance(item, Mapping):
            return {str(key): normalize(item[key]) for key in sorted(item)}
        if isinstance(item, list):
            return [normalize(entry) for entry in item]
        if isinstance(item, float) and item.is_integer():
            return int(item)
        return item

    canonical = json.dumps(normalize(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class BusEnvelopeVerifier:
    """Authenticate and bind command envelopes before simulated execution.

    The shared HMAC is deliberately a local-reference mechanism. A fielded
    adapter replaces it with workload identity, per-publisher keys, broker ACLs,
    and an approved cryptographic profile.
    """

    REQUIRED_FIELDS = {
        "messageId",
        "schemaVersion",
        "messageType",
        "source",
        "classification",
        "timeCreated",
        "signature",
        "payload",
    }

    def __init__(
        self,
        secret: str,
        authority_node_id: str,
        *,
        max_age_seconds: float = 30.0,
        max_future_skew_seconds: float = 5.0,
    ) -> None:
        self.secret = secret.encode()
        self.authority_node_id = authority_node_id
        self.max_age_seconds = max_age_seconds
        self.max_future_skew_seconds = max_future_skew_seconds
        self.seen_message_ids: set[str] = set()

    def verify(
        self,
        data: bytes,
        *,
        expected_message_type: str,
        subject: str,
        subject_prefix: str,
        target_field: str,
        now: datetime | None = None,
    ) -> dict:
        raw = json.loads(data)
        if not isinstance(raw, dict):
            raise ValueError("command envelope must be a JSON object")
        missing = sorted(self.REQUIRED_FIELDS - raw.keys())
        if missing:
            raise ValueError(f"command envelope missing: {', '.join(missing)}")
        if raw["schemaVersion"] != "1.0.0":
            raise ValueError("unsupported command schemaVersion")
        if raw["messageType"] != expected_message_type:
            raise ValueError("command messageType does not match handler")
        source = raw.get("source")
        if not isinstance(source, dict):
            raise ValueError("command source is invalid")
        if (
            source.get("nodeId") != self.authority_node_id
            or source.get("componentType") != "c2"
        ):
            raise ValueError("command source is not the authoritative C2 node")
        supplied_signature = raw.get("signature")
        unsigned = {key: value for key, value in raw.items() if key != "signature"}
        canonical = json.dumps(
            unsigned,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        expected_signature = "hmac-sha256:" + hmac.new(
            self.secret, canonical, hashlib.sha256
        ).hexdigest()
        if not isinstance(supplied_signature, str) or not hmac.compare_digest(
            supplied_signature, expected_signature
        ):
            raise ValueError("command envelope signature is absent or invalid")
        message_id = raw["messageId"]
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("command messageId is invalid")
        if message_id in self.seen_message_ids:
            raise ValueError("command envelope replay detected")
        created = parse_utc(raw["timeCreated"])
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        age = (current - created).total_seconds()
        if age > self.max_age_seconds or age < -self.max_future_skew_seconds:
            raise ValueError("command envelope timestamp is outside the acceptance window")
        payload = raw["payload"]
        if not isinstance(payload, dict):
            raise ValueError("command payload is invalid")
        prefix = f"{subject_prefix}."
        if not subject.startswith(prefix) or "." in subject[len(prefix) :]:
            raise ValueError("command subject is invalid")
        subject_target = subject[len(prefix) :]
        if payload.get(target_field) != subject_target:
            raise ValueError("command subject and payload target do not match")
        self.seen_message_ids.add(message_id)
        if len(self.seen_message_ids) > 4096:
            # Bounded memory is sufficient because the timestamp window rejects
            # old traffic. Keep deterministic behavior for the reference model.
            self.seen_message_ids = set(sorted(self.seen_message_ids)[-3072:])
        return payload


@dataclass(frozen=True)
class AssetConfig:
    """Authoritative local scenario origin used by every simulated component."""

    lat: float = DEFAULT_ASSET_LAT
    lon: float = DEFAULT_ASSET_LON
    label: str = "NAS North Island (notional reference point)"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "AssetConfig":
        lat = float(env.get("SIM_ASSET_LAT", DEFAULT_ASSET_LAT))
        lon = float(env.get("SIM_ASSET_LON", DEFAULT_ASSET_LON))
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError("SIM_ASSET_LAT/SIM_ASSET_LON must be valid WGS84 coordinates")
        return cls(lat=lat, lon=lon, label=env.get("SIM_ASSET_LABEL", cls.label))


@dataclass
class SimulationClock:
    """Single monotonic clock for scenario movement, observations, and events."""

    start: datetime = DEFAULT_START_TIME
    elapsed_seconds: float = 0.0
    tick_sequence: int = 0

    def __post_init__(self) -> None:
        self.start = parse_utc(self.start)

    @property
    def now(self) -> datetime:
        return self.start + timedelta(seconds=self.elapsed_seconds)

    def advance(self, dt: float) -> datetime:
        if not math.isfinite(dt) or dt <= 0:
            raise ValueError("simulation time step must be finite and positive")
        self.elapsed_seconds += dt
        self.tick_sequence += 1
        return self.now


@dataclass(frozen=True)
class SensorSpec:
    """Notional sensor geometry and one-sigma measurement uncertainty."""

    sensor_id: str
    modality: str
    x: float
    y: float
    range_meters: float
    horizontal_sigma_meters: float
    vertical_sigma_meters: float
    latency_milliseconds: int


DEFAULT_SENSORS = (
    SensorSpec("SEN-RAD-01", "RADAR", 0, -150, 6000, 24, 35, 180),
    SensorSpec("SEN-RF-02", "RF", 1700, 1300, 5200, 65, 90, 350),
    SensorSpec("SEN-EO-03", "EO_IR", -1800, 1000, 3600, 18, 25, 240),
    SensorSpec("SEN-SHIP-04", "RADAR", 4800, -1200, 6500, 30, 42, 260),
    SensorSpec("SEN-MADIS-05", "RADAR", -200, -2600, 4500, 34, 48, 300),
)

BLUE_IDENTITIES = {"FRIEND", "ASSUMED_FRIEND", "NEUTRAL"}
HOSTILE_TYPES = ("MULTIROTOR", "UAS_GROUP_1", "UAS_GROUP_2", "FIXED_WING")


@dataclass
class Track:
    track_id: str
    identity: str
    x: float
    y: float
    speed: float
    track_quality: int
    classification: str
    heading: float
    emitter_state: str
    altitude_meters: float = 120.0
    platform: str | None = None
    service: str | None = None
    orbit_x: float = 0.0
    orbit_y: float = 0.0
    orbit_radius: float = 700.0
    orbit_speed: float = 0.25
    orbit_phase: float = 0.0
    armed: bool = False
    weapon_range_meters: float = 0.0
    weapon: str | None = None
    alive: bool = True
    contributing_sensors: list[str] = field(default_factory=list)
    horizontal_sigma_meters: float = 140.0
    vertical_sigma_meters: float = 180.0
    last_observed: datetime | None = None
    measurement_latency_milliseconds: int = 0
    observation_sequence: int = 0
    fusion_state: str = "TENTATIVE"

    def is_blue(self) -> bool:
        return self.identity in BLUE_IDENTITIES

    def move(self, dt: float) -> None:
        if self.is_blue():
            self.orbit_phase += dt * self.orbit_speed
            self.x = self.orbit_x + math.cos(self.orbit_phase) * self.orbit_radius
            self.y = self.orbit_y + math.sin(self.orbit_phase) * self.orbit_radius
            self.heading = (self.orbit_phase + math.pi / 2) % math.tau
            return

        desired = math.atan2(-self.y, -self.x)
        error = ((desired - self.heading + math.pi * 3) % math.tau) - math.pi
        self.heading += _clamp(error, -0.5 * dt, 0.5 * dt)
        self.x += math.cos(self.heading) * self.speed * dt
        self.y += math.sin(self.heading) * self.speed * dt

    def observe(
        self,
        sensors: tuple[SensorSpec, ...],
        clock: SimulationClock,
        dt: float,
        tasked_sensor_ids: set[str],
        search_volumes: Mapping[str, Mapping[str, float]] | None = None,
    ) -> None:
        contributors: list[SensorSpec] = []
        for sensor in sensors:
            if math.hypot(self.x - sensor.x, self.y - sensor.y) > sensor.range_meters:
                continue
            if sensor.modality == "RF" and self.emitter_state != "EMITTING":
                continue
            volume = (search_volumes or {}).get(sensor.sensor_id)
            if volume is not None:
                bearing = math.degrees(
                    math.atan2(self.y - sensor.y, self.x - sensor.x)
                ) % 360
                center = volume.get("centerBearingDeg", 0.0) % 360
                width = volume.get("widthDeg", 360.0)
                delta = abs(((bearing - center + 180) % 360) - 180)
                if delta > width / 2:
                    continue
                if self.altitude_meters < volume.get("minAltMeters", -math.inf):
                    continue
                if self.altitude_meters > volume.get("maxAltMeters", math.inf):
                    continue
            contributors.append(sensor)
        self.contributing_sensors = [sensor.sensor_id for sensor in contributors]

        if contributors:
            horizontal_information = 0.0
            vertical_information = 0.0
            weighted_latency = 0.0
            weight_total = 0.0
            for sensor in contributors:
                # A current task changes the next observation's integration quality;
                # it never directly increments track quality.
                task_factor = 0.72 if sensor.sensor_id in tasked_sensor_ids else 1.0
                horizontal_sigma = sensor.horizontal_sigma_meters * task_factor
                vertical_sigma = sensor.vertical_sigma_meters * task_factor
                horizontal_information += 1.0 / horizontal_sigma**2
                vertical_information += 1.0 / vertical_sigma**2
                weight = 1.0 / horizontal_sigma**2
                weighted_latency += sensor.latency_milliseconds * weight
                weight_total += weight

            self.horizontal_sigma_meters = max(3.0, math.sqrt(1.0 / horizontal_information))
            self.vertical_sigma_meters = max(5.0, math.sqrt(1.0 / vertical_information))
            self.measurement_latency_milliseconds = round(weighted_latency / weight_total)
            self.last_observed = clock.now - timedelta(milliseconds=self.measurement_latency_milliseconds)
            self.observation_sequence += 1
            age_seconds = self.measurement_latency_milliseconds / 1000.0
            source_bonus = min(3, len(contributors) - 1)
            precision_penalty = max(0, math.ceil((self.horizontal_sigma_meters - 8.0) / 8.0))
            age_penalty = math.floor(age_seconds / 1.5)
            self.track_quality = int(_clamp(12 + source_bonus - precision_penalty - age_penalty, 0, 15))
            self.fusion_state = "CONFIRMED" if self.track_quality >= 6 else "TENTATIVE"
            return

        # With no new observation the fusion track coasts and uncertainty grows.
        self.horizontal_sigma_meters = min(1000.0, self.horizontal_sigma_meters + 12.0 * dt)
        self.vertical_sigma_meters = min(1500.0, self.vertical_sigma_meters + 18.0 * dt)
        if self.last_observed is None:
            self.track_quality = int(_clamp(self.track_quality - dt * 0.6, 0, 15))
            self.fusion_state = "TENTATIVE"
            return
        age_seconds = self.data_age_seconds(clock.now)
        self.track_quality = int(_clamp(self.track_quality - dt * 0.6, 0, 15))
        self.fusion_state = "COASTING" if age_seconds <= 6.0 else "STALE"

    def data_age_seconds(self, now: datetime) -> float:
        if self.last_observed is None:
            return 0.0
        return max(0.0, (now - self.last_observed).total_seconds())

    def range_to_asset(self) -> float:
        return math.hypot(self.x, self.y)

    def payload(self, asset: AssetConfig, clock: SimulationClock, ttl_seconds: float) -> dict[str, Any]:
        meters_per_degree_latitude = 111_320.0
        meters_per_degree_longitude = 111_320.0 * math.cos(math.radians(asset.lat))
        observed = self.last_observed or clock.now
        horizontal_variance = round(self.horizontal_sigma_meters**2, 2)
        vertical_variance = round(self.vertical_sigma_meters**2, 2)
        payload: dict[str, Any] = {
            "trackId": self.track_id,
            "kinematics": {
                "position": {
                    "lat": asset.lat + self.y / meters_per_degree_latitude,
                    "lon": asset.lon + self.x / meters_per_degree_longitude,
                    "altMeters": round(self.altitude_meters, 1),
                    "frame": "WGS84",
                    "altitudeReference": "MSL",
                },
                "velocity": {
                    "speedMps": round(self.speed, 1),
                    "courseDeg": round(math.degrees(self.heading) % 360, 1) % 360,
                    "verticalRateMps": 0.0,
                },
            },
            "covariance": {
                "referenceFrame": "LOCAL_ENU",
                "confidenceLevel": "ONE_SIGMA",
                "eastVarianceM2": horizontal_variance,
                "northVarianceM2": horizontal_variance,
                "upVarianceM2": vertical_variance,
                "eastNorthCovarianceM2": 0.0,
                # Retained for backward-compatible consumers; explicitly one sigma.
                "horizontalMeters": round(self.horizontal_sigma_meters, 1),
                "verticalMeters": round(self.vertical_sigma_meters, 1),
            },
            "trackQuality": self.track_quality,
            "identity": self.identity,
            "emitterState": self.emitter_state,
            "classificationType": self.classification,
            "contributingSensors": list(self.contributing_sensors),
            "timeObserved": observed.isoformat(),
            "timeUpdated": clock.now.isoformat(),
            "dataAgeSeconds": round(self.data_age_seconds(clock.now), 3),
            "measurementLatencyMilliseconds": self.measurement_latency_milliseconds,
            "observationSequence": self.observation_sequence,
            "fusionState": self.fusion_state,
            "timeToLiveSeconds": ttl_seconds,
            "modelProvenance": NOTIONAL_MODEL_NOTICE,
        }
        if self.platform:
            payload["platform"] = self.platform
        if self.service:
            payload["service"] = self.service
        return payload


class Scenario:
    """Seeded scenario whose state advances only through :meth:`tick`."""

    BLUE_ROSTER = (
        ("FRIEND", 3300, -900, 600, 0.28, "ROTARY", 70, "MH-60R", "USN", 300, True, 2600, "AGM-114"),
        ("FRIEND", 1900, 2500, 1500, 0.20, "FIXED_WING", 180, "F/A-18E", "USN", 6000, True, 3200, "AIM-9X"),
        ("FRIEND", -2600, -1500, 700, 0.22, "ROTARY", 120, "MV-22B", "USMC", 900, False, 0, None),
        ("FRIEND", -1200, 1500, 500, 0.30, "ROTARY", 80, "AH-1Z", "USMC", 150, True, 2400, "AGM-114"),
        ("ASSUMED_FRIEND", -2700, 2000, 900, 0.18, "UAS_GROUP_3", 40, "RQ-21A", "USMC", 1500, False, 0, None),
        ("FRIEND", 300, 2800, 600, 0.30, "UAS_GROUP_3", 45, "RQ-7B", "USA", 2400, False, 0, None),
        ("FRIEND", -2300, 300, 550, 0.26, "ROTARY", 75, "UH-60M", "USA", 250, False, 0, None),
        ("FRIEND", 2700, -2500, 1300, 0.14, "FIXED_WING", 90, "MQ-9", "USAF", 7600, True, 2800, "AGM-114"),
    )

    def __init__(
        self,
        *,
        seed: int = 4242,
        start_time: datetime = DEFAULT_START_TIME,
        asset: AssetConfig | None = None,
        sensors: tuple[SensorSpec, ...] = DEFAULT_SENSORS,
        ttl_seconds: float = 6.0,
        enable_organic_air_defense: bool = False,
    ) -> None:
        self.seed = seed
        self.rng = random.Random(seed)
        self.clock = SimulationClock(start_time)
        self.asset = asset or AssetConfig()
        self.sensors = sensors
        self.ttl_seconds = ttl_seconds
        self.enable_organic_air_defense = enable_organic_air_defense
        self.tracks: dict[str, Track] = {}
        self.leakers: list[str] = []
        self._track_sequence = 1000
        self._wave_elapsed = 0.0
        self._tasked_until: dict[tuple[str, str], float] = {}
        self._search_volumes: dict[str, dict[str, float]] = {}
        self.track_custody: dict[str, str] = {}
        for row in self.BLUE_ROSTER:
            self._spawn_blue(*row)
        self.spawn_hostile(bearing=-0.6, range_meters=4200, track_quality=5, classification="MULTIROTOR")
        self.spawn_wave(6)

    def _next_track_id(self) -> str:
        self._track_sequence += 1
        return f"TRK-{self._track_sequence}"

    def _spawn_blue(
        self,
        identity: str,
        orbit_x: float,
        orbit_y: float,
        orbit_radius: float,
        orbit_speed: float,
        classification: str,
        speed: float,
        platform: str | None,
        service: str,
        altitude_meters: float,
        armed: bool,
        weapon_range_meters: float,
        weapon: str | None,
    ) -> Track:
        track = Track(
            track_id=self._next_track_id(),
            identity=identity,
            x=orbit_x + orbit_radius,
            y=orbit_y,
            speed=speed,
            track_quality=13,
            classification=classification,
            heading=math.pi / 2,
            emitter_state="EMITTING",
            altitude_meters=altitude_meters,
            platform=platform,
            service=service,
            orbit_x=orbit_x,
            orbit_y=orbit_y,
            orbit_radius=orbit_radius,
            orbit_speed=orbit_speed,
            armed=armed,
            weapon_range_meters=weapon_range_meters,
            weapon=weapon,
        )
        self.tracks[track.track_id] = track
        return track

    def spawn_hostile(
        self,
        *,
        bearing: float | None = None,
        range_meters: float | None = None,
        track_quality: int = 4,
        classification: str | None = None,
        speed: float | None = None,
    ) -> Track:
        bearing = self.rng.uniform(0, math.tau) if bearing is None else bearing
        range_meters = self.rng.uniform(4600, 5200) if range_meters is None else range_meters
        track = Track(
            track_id=self._next_track_id(),
            identity="HOSTILE",
            x=math.cos(bearing) * range_meters,
            y=math.sin(bearing) * range_meters,
            speed=self.rng.uniform(26, 38) if speed is None else speed,
            track_quality=track_quality,
            classification=classification or self.rng.choice(HOSTILE_TYPES),
            heading=math.atan2(-math.sin(bearing), -math.cos(bearing)),
            emitter_state="SILENT" if self.rng.random() < 0.25 else "EMITTING",
            altitude_meters=self.rng.uniform(60, 400),
        )
        self.tracks[track.track_id] = track
        return track

    def spawn_wave(self, count: int) -> list[Track]:
        bearing = self.rng.uniform(0, math.tau)
        return [
            self.spawn_hostile(
                bearing=bearing + (index - count / 2) * 0.09,
                range_meters=4900 + (index % 3) * 160,
                track_quality=4,
                classification="UAS_GROUP_1",
            )
            for index in range(count)
        ]

    def tick(self, dt: float) -> None:
        self.clock.advance(dt)
        self._wave_elapsed += dt
        hostiles = [track for track in self.tracks.values() if track.identity == "HOSTILE"]
        if self._wave_elapsed > 30 and len(hostiles) < 10:
            self._wave_elapsed = 0.0
            self.spawn_wave(self.rng.randint(2, 4))

        for track in list(self.tracks.values()):
            track.move(dt)
            tasked = {
                sensor_id
                for (track_id, sensor_id), deadline in self._tasked_until.items()
                if track_id == track.track_id and deadline >= self.clock.elapsed_seconds
            }
            track.observe(
                self.sensors,
                self.clock,
                dt,
                tasked,
                self._search_volumes,
            )
            if track.last_observed and track.data_age_seconds(self.clock.now) > self.ttl_seconds:
                self.tracks.pop(track.track_id, None)
                continue
            if not track.is_blue() and track.range_to_asset() < 250:
                self.leakers.append(track.track_id)
                self.tracks.pop(track.track_id, None)

        self._tasked_until = {
            key: deadline
            for key, deadline in self._tasked_until.items()
            if deadline >= self.clock.elapsed_seconds
        }
        self._search_volumes = {
            sensor_id: volume
            for sensor_id, volume in self._search_volumes.items()
            if volume["expiresAtElapsed"] >= self.clock.elapsed_seconds
        }
        if self.enable_organic_air_defense:
            self._notional_organic_air_defense()

    def _notional_organic_air_defense(self) -> None:
        """Optional exercise-control behavior; disabled by default.

        It is intentionally not treated as a C2-authorized engagement and should
        only be enabled by explicit exercise configuration.
        """
        hostiles = [track for track in self.tracks.values() if track.identity == "HOSTILE"]
        for blue in (track for track in self.tracks.values() if track.is_blue() and track.armed):
            candidate = min(
                hostiles,
                key=lambda target: math.hypot(blue.x - target.x, blue.y - target.y),
                default=None,
            )
            if candidate and math.hypot(blue.x - candidate.x, blue.y - candidate.y) < blue.weapon_range_meters:
                # One deterministic exercise event per tick; no claimed Pk.
                self.tracks.pop(candidate.track_id, None)
                break

    def task(self, track_id: str, sensor_id: str, duration_seconds: float = 4.0) -> tuple[int, int] | None:
        track = self.tracks.get(track_id)
        sensor = next((item for item in self.sensors if item.sensor_id == sensor_id), None)
        if track is None or sensor is None:
            return None
        if math.hypot(track.x - sensor.x, track.y - sensor.y) > sensor.range_meters:
            return None
        before = track.track_quality
        self._tasked_until[(track_id, sensor_id)] = self.clock.elapsed_seconds + duration_seconds
        return before, track.track_quality

    def set_search_volume(
        self,
        sensor_id: str,
        volume: Mapping[str, float],
        duration_seconds: float = 10.0,
    ) -> bool:
        if not any(sensor.sensor_id == sensor_id for sensor in self.sensors):
            return False
        width = float(volume.get("widthDeg", 360.0))
        if not 0 < width <= 360:
            return False
        minimum = float(volume.get("minAltMeters", -1_000_000.0))
        maximum = float(volume.get("maxAltMeters", 1_000_000.0))
        if minimum > maximum:
            return False
        self._search_volumes[sensor_id] = {
            "centerBearingDeg": float(volume.get("centerBearingDeg", 0.0)) % 360,
            "widthDeg": width,
            "minAltMeters": minimum,
            "maxAltMeters": maximum,
            "expiresAtElapsed": self.clock.elapsed_seconds + duration_seconds,
        }
        return True

    def handoff(self, track_id: str, from_sensor_id: str, to_sensor_id: str) -> bool:
        if not any(sensor.sensor_id == from_sensor_id for sensor in self.sensors):
            return False
        if self.task(track_id, to_sensor_id) is None:
            return False
        self.track_custody[track_id] = to_sensor_id
        return True

    def neutralize(self, track_id: str) -> bool:
        return self.tracks.pop(track_id, None) is not None

    def track_payloads(self) -> list[dict[str, Any]]:
        return [
            track.payload(self.asset, self.clock, self.ttl_seconds)
            for track in sorted(self.tracks.values(), key=lambda item: item.track_id)
            if track.last_observed is not None
        ]


@dataclass(frozen=True)
class TokenValidation:
    valid: bool
    reason: str
    claims: dict[str, Any] | None = None


class AuthorityTokenVerifier:
    """Independent effector-side verifier for the reference compact token.

    Format: ``v1.<base64url canonical JSON claims>.<base64url HMAC-SHA256>``.
    The verifier intentionally owns its replay cache; it does not trust the C2
    node's record that a token was issued or consumed.
    """

    REQUIRED_CLAIMS = {
        "jti",
        "iss",
        "sub",
        "engagementId",
        "requestId",
        "trackId",
        "effectorId",
        "engagementType",
        "policyVersion",
        "weaponsControlStatus",
        "trackSnapshotTimeObserved",
        "orderSequence",
        "constraintsHash",
        "iat",
        "exp",
    }
    REQUIRED_ABORT_CLAIMS = {
        "jti",
        "iss",
        "sub",
        "engagementId",
        "requestId",
        "trackId",
        "effectorId",
        "action",
        "policyVersion",
        "weaponsControlStatus",
        "directiveSequence",
        "reasonHash",
        "iat",
        "exp",
    }

    def __init__(self, secret: str | bytes, issuer: str | None = None) -> None:
        key = secret.encode() if isinstance(secret, str) else secret
        self._key = key if len(key) >= 32 else hashlib.sha256(key).digest()
        self.issuer = issuer
        self._consumed: set[str] = set()

    @staticmethod
    def _decode(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    def _verify_token(
        self,
        token: Any,
        required_claims: set[str],
        now: datetime | None,
    ) -> TokenValidation:
        if not isinstance(token, str):
            return TokenValidation(False, "INTERLOCK_BLOCKED")
        if not 20 <= len(token) <= 4096:
            return TokenValidation(False, "MALFORMED_AUTHORITY_TOKEN")
        try:
            version, encoded, supplied_signature = token.split(".", 2)
            if version != "v1":
                return TokenValidation(False, "UNSUPPORTED_TOKEN_VERSION")
            expected_signature = self._encode(
                hmac.new(self._key, f"v1.{encoded}".encode(), hashlib.sha256).digest()
            )
            if not hmac.compare_digest(supplied_signature, expected_signature):
                return TokenValidation(False, "INVALID_AUTHORITY_SIGNATURE")
            claims = json.loads(self._decode(encoded))
            if not isinstance(claims, dict) or not required_claims.issubset(claims):
                return TokenValidation(False, "INVALID_AUTHORITY_CLAIMS")
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, base64.binascii.Error):
            return TokenValidation(False, "MALFORMED_AUTHORITY_TOKEN")

        now_epoch = int((now or datetime.now(timezone.utc)).timestamp())
        if not isinstance(claims["iat"], int) or not isinstance(claims["exp"], int):
            return TokenValidation(False, "INVALID_AUTHORITY_CLAIMS")
        for claim_name in required_claims - {"iat", "exp"}:
            if not isinstance(claims[claim_name], str) or not claims[claim_name]:
                return TokenValidation(False, "INVALID_AUTHORITY_CLAIMS")
        if claims["iat"] > now_epoch + 5:
            return TokenValidation(False, "TOKEN_NOT_YET_VALID")
        if claims["exp"] <= now_epoch:
            return TokenValidation(False, "TOKEN_EXPIRED")
        if claims["exp"] - claims["iat"] > 300:
            return TokenValidation(False, "TOKEN_LIFETIME_EXCEEDED")
        if self.issuer and claims["iss"] != self.issuer:
            return TokenValidation(False, "WRONG_AUTHORITY_ISSUER")
        if claims["jti"] in self._consumed:
            return TokenValidation(False, "DUPLICATE_ORDER")
        return TokenValidation(True, "OK", claims)

    def validate(self, order: Mapping[str, Any], now: datetime | None = None) -> TokenValidation:
        verified = self._verify_token(order.get("authorityToken"), self.REQUIRED_CLAIMS, now)
        if not verified.valid or verified.claims is None:
            return verified
        claims = verified.claims

        expected_scope = {
            "engagementId": order.get("engagementId"),
            "requestId": order.get("requestId"),
            "trackId": order.get("trackId"),
            "effectorId": order.get("effectorId"),
            "engagementType": order.get("engagementType"),
        }
        for name, expected in expected_scope.items():
            if not isinstance(expected, str) or claims.get(name) != expected:
                return TokenValidation(False, "TOKEN_SCOPE_MISMATCH")

        if claims.get("orderSequence") != str(order.get("orderSequence")):
            return TokenValidation(False, "TOKEN_SCOPE_MISMATCH")
        constraints = order.get("constraints")
        if not isinstance(constraints, Mapping):
            return TokenValidation(False, "INVALID_AUTHORITY_CLAIMS")
        constraints_hash = canonical_safety_hash(constraints)
        if claims.get("constraintsHash") != constraints_hash:
            return TokenValidation(False, "TOKEN_SCOPE_MISMATCH")

        # RFC3339 permits equivalent UTC spellings (`Z` and `+00:00`). Pydantic's
        # JSON serializer may normalize the wire order even though the token was
        # minted from `datetime.isoformat()`, so compare instants, not raw text.
        try:
            if parse_utc(claims["trackSnapshotTimeObserved"]) != parse_utc(
                order.get("trackSnapshotTimeObserved")
            ):
                return TokenValidation(False, "TOKEN_SCOPE_MISMATCH")
        except (TypeError, ValueError):
            return TokenValidation(False, "INVALID_AUTHORITY_CLAIMS")

        declared_expiry = order.get("authorityTokenExpiresAt")
        if declared_expiry:
            try:
                if int(parse_utc(declared_expiry).timestamp()) != claims["exp"]:
                    return TokenValidation(False, "TOKEN_SCOPE_MISMATCH")
            except (TypeError, ValueError):
                return TokenValidation(False, "INVALID_AUTHORITY_CLAIMS")
        declared_scope = order.get("authorityTokenScope")
        if declared_scope:
            if not isinstance(declared_scope, Mapping):
                return TokenValidation(False, "INVALID_AUTHORITY_CLAIMS")
            for name in (
                "engagementId",
                "requestId",
                "trackId",
                "effectorId",
                "engagementType",
                "policyVersion",
                "weaponsControlStatus",
            ):
                if declared_scope.get(name) != claims.get(name):
                    return TokenValidation(False, "TOKEN_SCOPE_MISMATCH")
        return TokenValidation(True, "OK", claims)

    def validate_abort(self, directive: Mapping[str, Any], now: datetime | None = None) -> TokenValidation:
        verified = self._verify_token(
            directive.get("authorityToken"), self.REQUIRED_ABORT_CLAIMS, now
        )
        if not verified.valid or verified.claims is None:
            return verified
        claims = verified.claims
        expected_scope = {
            "engagementId": directive.get("engagementId"),
            "requestId": directive.get("requestId"),
            "trackId": directive.get("trackId"),
            "effectorId": directive.get("effectorId"),
            "action": directive.get("action"),
        }
        for name, expected in expected_scope.items():
            if not isinstance(expected, str) or claims.get(name) != expected:
                return TokenValidation(False, "TOKEN_SCOPE_MISMATCH")
        if claims.get("directiveSequence") != str(directive.get("directiveSequence")):
            return TokenValidation(False, "TOKEN_SCOPE_MISMATCH")
        reason = directive.get("reason")
        if not isinstance(reason, str) or claims.get("reasonHash") != hashlib.sha256(
            reason.encode()
        ).hexdigest():
            return TokenValidation(False, "TOKEN_SCOPE_MISMATCH")
        if claims.get("action") != "ABORT":
            return TokenValidation(False, "TOKEN_SCOPE_MISMATCH")
        declared_expiry = directive.get("authorityTokenExpiresAt")
        if declared_expiry:
            try:
                if int(parse_utc(declared_expiry).timestamp()) != claims["exp"]:
                    return TokenValidation(False, "TOKEN_SCOPE_MISMATCH")
            except (TypeError, ValueError):
                return TokenValidation(False, "INVALID_AUTHORITY_CLAIMS")
        return TokenValidation(True, "OK", claims)

    def consume(self, validation: TokenValidation) -> None:
        if not validation.valid or validation.claims is None:
            raise ValueError("cannot consume an invalid authority token")
        self._consumed.add(str(validation.claims["jti"]))


@dataclass
class EffectorModel:
    """Inventory and readiness for one explicitly notional simulated effector."""

    effector_id: str
    effector_type: str = "EW_JAMMER"
    supported_effects: frozenset[str] | None = None
    capacity: int = 12
    remaining: int = 12
    readiness: str = "READY"
    active_engagement_id: str | None = None
    min_range_meters: float = 0.0
    max_range_meters: float = 5000.0
    min_alt_meters: float = 0.0
    max_alt_meters: float = 1200.0

    def __post_init__(self) -> None:
        if self.capacity < 0 or not 0 <= self.remaining <= self.capacity:
            raise ValueError("effector remaining inventory must be between zero and capacity")
        if self.supported_effects is None:
            by_type = {
                "EW_JAMMER": frozenset({"EW_DEFEAT"}),
                "RF_TAKEOVER": frozenset({"RF_TAKEOVER"}),
                "KINETIC_GUN": frozenset({"KINETIC"}),
                "KINETIC_INTERCEPTOR": frozenset({"KINETIC"}),
                "DIRECTED_ENERGY": frozenset({"DIRECTED_ENERGY"}),
                "NET_CAPTURE": frozenset({"NET_CAPTURE"}),
            }
            self.supported_effects = by_type.get(self.effector_type, frozenset())

    def status_payload(self, asset: AssetConfig, time_reported: datetime) -> dict[str, Any]:
        return {
            "effectorId": self.effector_id,
            "effectorType": self.effector_type,
            "vendor": "REFERENCE-SIM",
            "readiness": self.readiness,
            "magazine": {"remaining": self.remaining, "capacity": self.capacity, "unit": "notional-effects"},
            "engagementEnvelope": {
                "location": {
                    "lat": asset.lat,
                    "lon": asset.lon,
                    "altMeters": 0.0,
                    "frame": "WGS84",
                    "altitudeReference": "MSL",
                },
                "minRangeMeters": self.min_range_meters,
                "maxRangeMeters": self.max_range_meters,
                "minAltMeters": self.min_alt_meters,
                "maxAltMeters": self.max_alt_meters,
            },
            "humanControl": "IN_THE_LOOP",
            "softwareVersion": "reference-sim-2",
            "timeReported": time_reported.isoformat(),
            "modelProvenance": NOTIONAL_MODEL_NOTICE,
        }


ReportCallback = Callable[[dict[str, Any]], Awaitable[None]]
SleepCallback = Callable[[float], Awaitable[None]]


class EngagementSimulator:
    """Validated, sequenced effect delivery with an explicit BDA interval."""

    # Illustrative delays and outcomes only; never field performance values.
    NOTIONAL_TIMING = {
        "EW_DEFEAT": (2.5, 2.0, 0.72),
        "RF_TAKEOVER": (3.5, 2.0, 0.68),
        "KINETIC": (1.5, 3.0, 0.76),
        "DIRECTED_ENERGY": (2.8, 2.5, 0.70),
        "NET_CAPTURE": (4.0, 2.5, 0.64),
    }

    def __init__(
        self,
        effector: EffectorModel,
        verifier: AuthorityTokenVerifier,
        *,
        seed: int = 4242,
        time_scale: float = 1.0,
        max_track_age_seconds: float = 6.0,
    ) -> None:
        self.effector = effector
        self.verifier = verifier
        self.seed = seed
        self.time_scale = max(0.0, time_scale)
        self.max_track_age_seconds = max_track_age_seconds
        self._active_track_id: str | None = None
        self._abort_requests: dict[str, tuple[int, str]] = {}
        self._attempts_by_track: dict[str, int] = {}

    def request_abort(
        self,
        directive: Mapping[str, Any],
        now: datetime,
    ) -> TokenValidation:
        """Validate and queue an abort for the active engagement.

        The execution coroutine observes the request between sleep quanta and
        publishes the terminal ABORTED state itself, preventing a later COMPLETE.
        """
        validation = self.verifier.validate_abort(directive, now)
        if not validation.valid:
            return validation
        if (
            directive.get("engagementId") != self.effector.active_engagement_id
            or directive.get("trackId") != self._active_track_id
        ):
            return TokenValidation(False, "ENGAGEMENT_NOT_ACTIVE")
        self.verifier.consume(validation)
        self._abort_requests[str(directive["engagementId"])] = (
            int(directive.get("directiveSequence", 1)),
            str(directive.get("reason", "operator abort")),
        )
        return validation

    def _deterministic_outcome(self, stable_event_key: str, probability: float) -> bool:
        digest = hashlib.sha256(f"{self.seed}:{stable_event_key}".encode()).digest()
        sample = int.from_bytes(digest[:8], "big") / float(2**64)
        return sample < probability

    async def execute(
        self,
        order: Mapping[str, Any],
        scenario: Scenario,
        report: ReportCallback,
        sleep: SleepCallback,
    ) -> None:
        engagement_id = str(order.get("engagementId", "UNKNOWN"))
        track_id = str(order.get("trackId", ""))
        effect = str(order.get("engagementType", ""))
        raw_sequence = order.get("orderSequence", 1)
        sequence = (
            raw_sequence
            if isinstance(raw_sequence, int) and not isinstance(raw_sequence, bool) and raw_sequence >= 1
            else 1
        )

        async def emit(
            state: str,
            reason: str = "OK",
            detail: str = "",
            *,
            terminal: bool = False,
            assessment: dict[str, Any] | None = None,
        ) -> None:
            nonlocal sequence
            sequence += 1
            payload: dict[str, Any] = {
                "engagementId": engagement_id,
                "effectorId": self.effector.effector_id,
                "trackId": track_id,
                "state": state,
                "sequence": sequence,
                "terminal": terminal,
                "reasonCode": reason,
                "detail": detail,
                "inventoryRemaining": self.effector.remaining,
                "timeReported": scenario.clock.now.isoformat(),
            }
            if assessment is not None:
                payload["effectAssessment"] = assessment
            await report(payload)

        async def finish_abort() -> bool:
            nonlocal sequence
            request = self._abort_requests.pop(engagement_id, None)
            if request is None:
                return False
            directive_sequence, reason = request
            sequence = max(sequence, directive_sequence)
            self.effector.active_engagement_id = None
            self._active_track_id = None
            self.effector.readiness = "READY" if self.effector.remaining > 0 else "OFFLINE"
            await emit(
                "ABORTED",
                "OPERATOR_ABORT",
                f"abort accepted by effector: {reason}",
                terminal=True,
                assessment={
                    "outcome": "INDETERMINATE",
                    "confidence": 0.0,
                    "method": "ABORT_BEFORE_TERMINAL_BDA",
                    "timeAssessed": scenario.clock.now.isoformat(),
                },
            )
            return True

        async def wait_interruptibly(duration: float) -> bool:
            remaining = duration
            while remaining > 0:
                quantum = min(0.1, remaining)
                await sleep(quantum)
                remaining -= quantum
                if await finish_abort():
                    return True
            return await finish_abort()

        if order.get("effectorId") != self.effector.effector_id:
            await emit("DENIED", "TOKEN_SCOPE_MISMATCH", "order is addressed to a different effector", terminal=True)
            return
        if raw_sequence != sequence:
            await emit("DENIED", "INVALID_ORDER", "orderSequence must be a positive integer", terminal=True)
            return
        validation = self.verifier.validate(order, scenario.clock.now)
        if not validation.valid:
            await emit("DENIED", validation.reason, "effector-side authority verification failed", terminal=True)
            return
        # A correctly signed/scoped order is one-use even when a later local
        # safety/interlock check rejects it. Otherwise the same order could become
        # executable later within its TTL after readiness/track state changes.
        self.verifier.consume(validation)
        track = scenario.tracks.get(track_id)
        if track is None:
            await emit("FAILED", "TRACK_NOT_FOUND", "target is no longer present in the local picture", terminal=True)
            return
        snapshot = order.get("trackSnapshotTimeObserved")
        try:
            snapshot_age = (
                (scenario.clock.now - parse_utc(snapshot)).total_seconds()
                if snapshot
                else self.max_track_age_seconds + 1
            )
        except (TypeError, ValueError):
            snapshot_age = self.max_track_age_seconds + 1
        if snapshot_age < -1 or snapshot_age > self.max_track_age_seconds:
            await emit("DENIED", "TRACK_STALE", "order references a stale or missing track observation", terminal=True)
            return
        if track.is_blue():
            await emit("DENIED", "INTERLOCK_BLOCKED", "local combat-identification interlock", terminal=True)
            return
        range_meters = track.range_to_asset()
        if not self.effector.min_range_meters <= range_meters <= self.effector.max_range_meters:
            await emit("DENIED", "OUT_OF_ENVELOPE", "target is outside the local effector range envelope", terminal=True)
            return
        if not self.effector.min_alt_meters <= track.altitude_meters <= self.effector.max_alt_meters:
            await emit("DENIED", "OUT_OF_ENVELOPE", "target is outside the local effector altitude envelope", terminal=True)
            return
        if effect not in self.effector.supported_effects:
            await emit("DENIED", "UNSUPPORTED_EFFECT", "requested effect is not supported by this effector", terminal=True)
            return
        if effect in {"EW_DEFEAT", "RF_TAKEOVER"} and track.emitter_state != "EMITTING":
            await emit("FAILED", "EFFECT_NOT_APPLICABLE", "RF effect cannot be applied to a non-emitting track", terminal=True)
            return
        if self.effector.active_engagement_id is not None:
            await emit("FAILED", "EFFECTOR_UNAVAILABLE", "effector is already committed", terminal=True)
            return
        if self.effector.remaining <= 0:
            self.effector.readiness = "OFFLINE"
            await emit("FAILED", "MAGAZINE_EMPTY", "notional effect inventory exhausted", terminal=True)
            return

        active_seconds, assessment_seconds, probability = self.NOTIONAL_TIMING[effect]
        constraints = order.get("constraints") or {}
        if not isinstance(constraints, Mapping):
            await emit("DENIED", "INVALID_ORDER", "constraints must be an object", terminal=True)
            return
        if constraints.get("requireHumanConfirmation") is True and constraints.get("humanConfirmed") is not True:
            await emit("DENIED", "NOT_AUTHORIZED", "required human confirmation is not present in the signed order", terminal=True)
            return
        friendly_limit = constraints.get("abortIfFriendlyWithinMeters")
        if not isinstance(friendly_limit, (int, float)) or isinstance(friendly_limit, bool):
            await emit("DENIED", "INVALID_ORDER", "friendly-proximity constraint is missing or invalid", terminal=True)
            return
        if any(
            other.is_blue()
            and math.hypot(other.x - track.x, other.y - track.y) < float(friendly_limit)
            for other in scenario.tracks.values()
        ):
            await emit("DENIED", "FRIENDLY_PROXIMITY", "friendly track is inside the signed safety radius", terminal=True)
            return
        maximum_duration = constraints.get("maxEngagementSeconds")
        planned_duration = (0.5 + active_seconds + assessment_seconds) * self.time_scale
        if maximum_duration is not None and (
            not isinstance(maximum_duration, (int, float)) or isinstance(maximum_duration, bool)
        ):
            await emit("DENIED", "INVALID_ORDER", "maxEngagementSeconds must be numeric", terminal=True)
            return
        if maximum_duration is not None and planned_duration > maximum_duration:
            await emit(
                "DENIED",
                "ORDER_CONSTRAINT_UNSATISFIABLE",
                "notional delivery and BDA interval exceeds maxEngagementSeconds",
                terminal=True,
            )
            return

        self.effector.active_engagement_id = engagement_id
        self._active_track_id = track_id
        self.effector.readiness = "DEGRADED"
        self.effector.remaining -= 1
        attempt = self._attempts_by_track.get(track_id, 0) + 1
        self._attempts_by_track[track_id] = attempt
        stable_outcome_key = f"{track_id}:{effect}:{attempt}"
        await emit("ACCEPTED", detail="authority, scope, freshness, applicability, and inventory checks passed")
        if await wait_interruptibly(0.5 * self.time_scale):
            return
        await emit("ACTIVE", detail="notional effect delivery in progress")
        if await wait_interruptibly(active_seconds * self.time_scale):
            return
        await emit(
            "ASSESSING",
            detail="effect delivery ended; awaiting simulated battle-damage assessment",
            assessment={
                "outcome": "PENDING",
                "confidence": 0.0,
                "method": "SIMULATED_MULTI_SENSOR_BDA",
                "timeAssessed": scenario.clock.now.isoformat(),
            },
        )
        if await wait_interruptibly(assessment_seconds * self.time_scale):
            return

        confirmed = self._deterministic_outcome(stable_outcome_key, probability)
        outcome = "CONFIRMED_EFFECT" if confirmed else "NO_CONFIRMED_EFFECT"
        assessment = {
            "outcome": outcome,
            "confidence": 0.78 if confirmed else 0.62,
            "method": "SIMULATED_MULTI_SENSOR_BDA",
            "timeAssessed": scenario.clock.now.isoformat(),
        }
        if confirmed:
            scenario.neutralize(track_id)
        self.effector.active_engagement_id = None
        self._active_track_id = None
        self.effector.readiness = "READY" if self.effector.remaining > 0 else "OFFLINE"
        await emit(
            "COMPLETE",
            detail="engagement closed with explicit notional BDA; COMPLETE does not itself imply defeat",
            terminal=True,
            assessment=assessment,
        )
