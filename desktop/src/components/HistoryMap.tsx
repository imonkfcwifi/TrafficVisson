import { useEffect, useMemo, useRef } from "react";
import type {
  Feature,
  FeatureCollection,
  MultiLineString,
  Point,
} from "geojson";
import {
  GeoJSONSource,
  LngLatBounds,
  MapLibreMap,
  NavigationControl,
  type MapLayerMouseEvent,
} from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { MapPin, MapPinOff } from "lucide-react";
import type { GpxRoutePoint, GpxTrack } from "../gpsTracks";
import { interpolateGpxPosition } from "../gpsTracks";
import type { AnalysisCandidate, SourceLocation } from "../runtimeState";
import { Badge } from "./ui";

type CandidateMapFeature = Feature<
  Point,
  {
    candidateId: string;
    selected: boolean;
    approved: boolean;
  }
>;

function lineSegments(points: readonly GpxRoutePoint[]): number[][][] {
  const segments: number[][][] = [];
  let current: number[][] = [];
  for (const point of points) {
    if (point.gapFromPrevious && current.length > 0) {
      if (current.length > 1) segments.push(current);
      current = [];
    }
    current.push([point.longitude, point.latitude]);
  }
  if (current.length > 1) segments.push(current);
  return segments;
}

function routeFeature(points: readonly GpxRoutePoint[]): Feature<MultiLineString> {
  return {
    type: "Feature",
    properties: {},
    geometry: {
      type: "MultiLineString",
      coordinates: lineSegments(points),
    },
  };
}

function progressFeature(
  track: GpxTrack,
  videoTimeSeconds: number,
): Feature<MultiLineString> {
  const included = track.routePoints.filter(
    (point) => point.videoTimeSeconds <= videoTimeSeconds,
  );
  const position = interpolateGpxPosition(track, videoTimeSeconds);
  if (position) {
    const last = included[included.length - 1];
    if (
      !last ||
      last.latitude !== position.latitude ||
      last.longitude !== position.longitude
    ) {
      included.push({
        ...(last ?? track.routePoints[0]),
        sequence: (last?.sequence ?? -1) + 1,
        sourceSequence: (last?.sourceSequence ?? -1) + 1,
        videoTimeSeconds,
        elapsedSeconds: videoTimeSeconds - track.videoOffsetSeconds,
        latitude: position.latitude,
        longitude: position.longitude,
        altitudeMeters: position.altitudeMeters,
        speedKph: position.speedKph,
        gapFromPrevious: false,
        distanceFromPreviousMeters: 0,
      });
    }
  }
  return routeFeature(included);
}

function currentPointFeature(
  track: GpxTrack,
  videoTimeSeconds: number,
): FeatureCollection<Point> {
  const position = interpolateGpxPosition(track, videoTimeSeconds);
  return {
    type: "FeatureCollection",
    features: position
      ? [
          {
            type: "Feature",
            properties: {},
            geometry: {
              type: "Point",
              coordinates: [position.longitude, position.latitude],
            },
          },
        ]
      : [],
  };
}

function candidateFeatures(
  track: GpxTrack,
  candidates: readonly AnalysisCandidate[],
  selectedCandidateId: string,
  approvedCandidateIds: ReadonlySet<string>,
): FeatureCollection<Point> {
  const features: CandidateMapFeature[] = [];
  for (const candidate of candidates) {
    const position = interpolateGpxPosition(
      track,
      candidate.representativeTimestampSeconds,
    );
    if (!position) continue;
    features.push({
      type: "Feature",
      properties: {
        candidateId: candidate.candidateId,
        selected: candidate.candidateId === selectedCandidateId,
        approved: approvedCandidateIds.has(candidate.candidateId),
      },
      geometry: {
        type: "Point",
        coordinates: [position.longitude, position.latitude],
      },
    });
  }
  return {
    type: "FeatureCollection",
    features,
  };
}

export function HistoryMap({
  sourceFileName,
  candidateCount,
  sourceLocation,
  track,
  currentVideoTimeSeconds,
  candidates,
  selectedCandidateId,
  approvedCandidateIds,
  onSelectCandidate,
}: {
  sourceFileName: string;
  candidateCount: number;
  sourceLocation: SourceLocation | null;
  track: GpxTrack | null;
  currentVideoTimeSeconds: number;
  candidates: readonly AnalysisCandidate[];
  selectedCandidateId: string;
  approvedCandidateIds: ReadonlySet<string>;
  onSelectCandidate: (candidateId: string) => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const selectCandidateRef = useRef(onSelectCandidate);
  selectCandidateRef.current = onSelectCandidate;

  const mappedCandidateCount = useMemo(
    () =>
      track
        ? candidates.filter((candidate) =>
            interpolateGpxPosition(
              track,
              candidate.representativeTimestampSeconds,
            ),
          ).length
        : 0,
    [candidates, track],
  );

  useEffect(() => {
    if (!track || !containerRef.current) return undefined;
    const map = new MapLibreMap({
      container: containerRef.current,
      style: {
        version: 8,
        sources: {},
        layers: [
          {
            id: "background",
            type: "background",
            paint: { "background-color": "#dce6f2" },
          },
        ],
      },
      center: [
        track.routePoints[0].longitude,
        track.routePoints[0].latitude,
      ],
      zoom: 14,
      attributionControl: false,
      canvasContextAttributes: { antialias: true },
    });
    mapRef.current = map;
    map.dragRotate.disable();
    map.touchZoomRotate.disableRotation();
    map.addControl(
      new NavigationControl({
        showCompass: false,
        visualizePitch: false,
      }),
      "bottom-right",
    );
    map.on("load", () => {
      map.addSource("trip-route", {
        type: "geojson",
        data: routeFeature(track.routePoints),
      });
      map.addSource("trip-progress", {
        type: "geojson",
        data: progressFeature(track, currentVideoTimeSeconds),
      });
      map.addSource("trip-current", {
        type: "geojson",
        data: currentPointFeature(track, currentVideoTimeSeconds),
      });
      map.addSource("trip-candidates", {
        type: "geojson",
        data: candidateFeatures(
          track,
          candidates,
          selectedCandidateId,
          approvedCandidateIds,
        ),
      });
      map.addLayer({
        id: "trip-route-halo",
        type: "line",
        source: "trip-route",
        paint: {
          "line-color": "rgba(255,255,255,0.92)",
          "line-width": 8,
          "line-opacity": 0.86,
        },
        layout: { "line-cap": "round", "line-join": "round" },
      });
      map.addLayer({
        id: "trip-route-line",
        type: "line",
        source: "trip-route",
        paint: {
          "line-color": "#8fa4bd",
          "line-width": 4,
          "line-opacity": 0.9,
        },
        layout: { "line-cap": "round", "line-join": "round" },
      });
      map.addLayer({
        id: "trip-progress-line",
        type: "line",
        source: "trip-progress",
        paint: {
          "line-color": "#3158e8",
          "line-width": 5,
          "line-opacity": 1,
        },
        layout: { "line-cap": "round", "line-join": "round" },
      });
      map.addLayer({
        id: "trip-candidate-points",
        type: "circle",
        source: "trip-candidates",
        paint: {
          "circle-radius": [
            "case",
            ["boolean", ["get", "selected"], false],
            8,
            5,
          ],
          "circle-color": [
            "case",
            ["boolean", ["get", "selected"], false],
            "#10131a",
            ["boolean", ["get", "approved"], false],
            "#1f8a70",
            "#ffffff",
          ],
          "circle-stroke-color": [
            "case",
            ["boolean", ["get", "approved"], false],
            "#ffffff",
            "#3158e8",
          ],
          "circle-stroke-width": 2,
        },
      });
      map.addLayer({
        id: "trip-current-point",
        type: "circle",
        source: "trip-current",
        paint: {
          "circle-radius": 7,
          "circle-color": "#3158e8",
          "circle-stroke-color": "#ffffff",
          "circle-stroke-width": 3,
        },
      });
      const bounds = new LngLatBounds();
      for (const point of track.routePoints) {
        bounds.extend([point.longitude, point.latitude]);
      }
      if (!bounds.isEmpty()) {
        map.fitBounds(bounds, {
          padding: 38,
          maxZoom: 16,
          duration: 0,
        });
      }
      map.on("mouseenter", "trip-candidate-points", () => {
        map.getCanvas().style.cursor = "pointer";
      });
      map.on("mouseleave", "trip-candidate-points", () => {
        map.getCanvas().style.cursor = "";
      });
      map.on(
        "click",
        "trip-candidate-points",
        (event: MapLayerMouseEvent) => {
        const candidateId = event.features?.[0]?.properties?.candidateId;
        if (typeof candidateId === "string") {
          selectCandidateRef.current(candidateId);
        }
        },
      );
    });
    return () => {
      mapRef.current = null;
      map.remove();
    };
  }, [track?.trackId, track?.revision]);

  useEffect(() => {
    const map = mapRef.current;
    if (!track || !map?.isStyleLoaded()) return;
    const progressSource = map.getSource("trip-progress");
    const currentSource = map.getSource("trip-current");
    const candidateSource = map.getSource("trip-candidates");
    if (progressSource instanceof GeoJSONSource) {
      progressSource.setData(progressFeature(track, currentVideoTimeSeconds));
    }
    if (currentSource instanceof GeoJSONSource) {
      currentSource.setData(
        currentPointFeature(track, currentVideoTimeSeconds),
      );
    }
    if (candidateSource instanceof GeoJSONSource) {
      candidateSource.setData(
        candidateFeatures(
          track,
          candidates,
          selectedCandidateId,
          approvedCandidateIds,
        ),
      );
    }
  }, [
    approvedCandidateIds,
    candidates,
    currentVideoTimeSeconds,
    selectedCandidateId,
    track,
  ]);

  if (track) {
    return (
      <div
        className="history-map-wrap history-map-live"
        role="region"
        aria-label="GPX 주행 경로와 후보 위치"
      >
        <div
          ref={containerRef}
          className="history-map"
          data-testid="history-map"
        />
        <div className="map-caption">
          <Badge tone="accent">GPX 동기화</Badge>
          <span>
            {track.sourceName} · {track.pointCount.toLocaleString("ko-KR")}점 ·
            후보 {mappedCandidateCount}/{candidateCount} 위치
          </span>
        </div>
        <div className="map-legend" aria-label="지도 범례">
          <span><i className="map-legend__route" />전체 경로</span>
          <span><i className="map-legend__progress" />재생 진행</span>
          <span><i className="map-legend__candidate" />사건·후보</span>
        </div>
      </div>
    );
  }

  if (sourceLocation) {
    const coordinate = `${sourceLocation.latitude.toFixed(6)}, ${sourceLocation.longitude.toFixed(6)}`;
    return (
      <div className="history-map-wrap history-map-unavailable" role="status">
        <div className="history-map-unavailable__icon">
          <MapPin size={30} />
        </div>
        <Badge tone="accent">촬영 시작 위치 확인</Badge>
        <h2>시작 좌표만 기록되어 있습니다</h2>
        <p>
          영상 컨테이너에서 읽은 한 지점입니다. GPX를 연결하면 시간에 따른 실제
          경로와 후보 위치를 표시합니다.
        </p>
        <dl>
          <div>
            <dt>영상</dt>
            <dd>{sourceFileName}</dd>
          </div>
          <div>
            <dt>촬영 시작 좌표</dt>
            <dd className="mono">{coordinate}</dd>
          </div>
          <div>
            <dt>시간 기반 후보</dt>
            <dd>{candidateCount.toLocaleString("ko-KR")}건 · 위치 미확정</dd>
          </div>
        </dl>
      </div>
    );
  }

  return (
    <div className="history-map-wrap history-map-unavailable" role="status">
      <div className="history-map-unavailable__icon">
        <MapPinOff size={30} />
      </div>
      <Badge tone="warning">GPS 자료 없음</Badge>
      <h2>주행 경로를 표시할 수 없습니다</h2>
      <p>
        이 영상에는 연결된 GPS 원본이 없습니다. GPX를 연결하기 전에는 경로선,
        차량 위치, 사건 마커를 만들지 않습니다.
      </p>
      <dl>
        <div>
          <dt>영상</dt>
          <dd>{sourceFileName}</dd>
        </div>
        <div>
          <dt>시간 기반 후보</dt>
          <dd>{candidateCount.toLocaleString("ko-KR")}건</dd>
        </div>
        <div>
          <dt>현재 위치 근거</dt>
          <dd>없음</dd>
        </div>
      </dl>
    </div>
  );
}
