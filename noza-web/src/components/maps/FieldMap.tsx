"use client";

import { useEffect, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";

export default function FieldMap() {
  const mapRef = useRef<L.Map | null>(null);
  const mapContainerRef = useRef<HTMLDivElement>(null);
  const [drawingEnabled, setDrawingEnabled] = useState(false);

  useEffect(() => {
    if (!mapContainerRef.current || mapRef.current) return;

    // Initialize map centered on Rwanda
    const map = L.map(mapContainerRef.current).setView([-1.9403, 29.8739], 13);

    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: '© OpenStreetMap contributors',
      maxZoom: 19,
    }).addTo(map);

    // Add satellite layer option
    const satelliteLayer = L.tileLayer(
      "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
      {
        attribution: "© Esri",
        maxZoom: 19,
      }
    );

    const baseLayers = {
      "Street Map": map,
      "Satellite": satelliteLayer,
    };

    L.control.layers(baseLayers as any).addTo(map);

    mapRef.current = map;

    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  const enableDrawing = () => {
    setDrawingEnabled(!drawingEnabled);
    if (mapRef.current && !drawingEnabled) {
      alert("Click on the map to draw field boundaries");
    }
  };

  return (
    <div className="bg-white rounded-lg shadow-sm border h-[600px] flex flex-col">
      <div className="p-4 border-b flex justify-between items-center">
        <div>
          <h3 className="font-semibold text-lg">Field Mapping</h3>
          <p className="text-sm text-gray-600">Draw your farm field boundaries on the map</p>
        </div>
        <button
          onClick={enableDrawing}
          className={`px-4 py-2 rounded-lg font-medium transition-colors ${
            drawingEnabled
              ? "bg-red-600 text-white hover:bg-red-700"
              : "bg-green-600 text-white hover:bg-green-700"
          }`}
        >
          {drawingEnabled ? "Stop Drawing" : "Start Drawing"}
        </button>
      </div>
      <div ref={mapContainerRef} className="flex-1" />
    </div>
  );
}
