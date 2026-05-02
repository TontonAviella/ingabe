import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Accept } from 'react-dropzone';
import { useDropzone } from 'react-dropzone';
import { useNavigate, useParams } from 'react-router-dom';
import useWebSocket from 'react-use-websocket';
import MapLibreMap from './MapLibreMap';
import 'maplibre-gl/dist/maplibre-gl.css';
import { apiFetch, fetchMaybeAuth, getCachedToken, getJwt, isAuthConfigured, useIsReady, useIsSignedOut } from '@mundi/ee';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Map as MLMap } from 'maplibre-gl';
import { toast } from 'sonner';
import type { ErrorEntry, UploadingFile } from '../lib/frontend-types';
import type { Conversation, EphemeralAction, MapProject, MapTreeResponse, PostgresConnectionDetails, TileLayerUpdate } from '../lib/types';
import { usePersistedState } from '../lib/usePersistedState';

const DROPZONE_ACCEPT: Accept = {
  'application/geo+json': ['.geojson', '.json'],
  'application/vnd.google-earth.kml+xml': ['.kml'],
  'application/vnd.google-earth.kmz': ['.kmz'],
  'image/tiff': ['.tif', '.tiff'],
  'image/jpeg': ['.jpg', '.jpeg'],
  'image/png': ['.png'],
  'application/geopackage+sqlite3': ['.gpkg'],
  'application/octet-stream': ['.fgb', '.dem'],
  'application/zip': ['.zip'],
  'application/vnd.las': ['.las'],
  'application/las+zip': ['.laz'],
  'text/csv': ['.csv'],
};

export default function ProjectView() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const { projectId, versionIdParam } = useParams();

  if (!projectId) {
    throw new Error('No project ID');
  }

  // Gate all queries on Clerk auth readiness to prevent premature 401s
  const isReady = useIsReady();
  const isSignedOut = useIsSignedOut();

  // State for controlling sources (PostGIS connections) refetch interval
  const [sourcesRefetchInterval, setSourcesRefetchInterval] = useState<number | false>(false);

  // handle a single store of project<->map<->conversation data
  const { data: project } = useQuery({
    queryKey: ['project', projectId],
    queryFn: async () => {
      const res = await fetchMaybeAuth(`/api/projects/${projectId}`);
      if (res.status === 404) {
        // Either not found or not shared; surface cleanly
        throw new Error('Project not found');
      }
      if (!res.ok) {
        throw new Error(`Failed to fetch project: ${res.status}`);
      }
      return (await res.json()) as MapProject;
    },
    enabled: isReady,
    // Do not poll the project route; sources polling is handled below
    refetchInterval: false,
  });

  // Fetch project PostGIS sources and update refetch interval while documenting
  const { data: projectSources } = useQuery({
    queryKey: ['project', projectId, 'sources'],
    queryFn: async () => {
      const res = await apiFetch(`/api/projects/${projectId}/sources`);
      if (!res.ok) throw new Error('Failed to fetch project sources');
      return (await res.json()) as PostgresConnectionDetails[];
    },
    enabled: isReady,
    retry: 5,
    retryDelay: (attempt) => 1000 * attempt,
    // While any connection is still being documented, poll this endpoint
    refetchInterval: sourcesRefetchInterval,
  });

  useEffect(() => {
    // Poll only while there are connections actively documenting (no error yet)
    const hasLoadingConnections = (projectSources || []).some((c) => !c.is_documented && !c.last_error_text);
    setSourcesRefetchInterval(hasLoadingConnections ? 500 : false);
  }, [projectSources]);

  const [conversationId, setConversationId] = usePersistedState<number | null>('conversationId', [projectId], null);
  const { data: conversations, isError: conversationsError } = useQuery({
    queryKey: ['project', projectId, 'conversations'],
    queryFn: async () => {
      const res = await apiFetch(`/api/conversations?project_id=${projectId}`);
      if (!res.ok) throw new Error('Failed to fetch conversations');
      return (await res.json()) as Conversation[];
    },
    enabled: isReady,
    retry: 5,
    retryDelay: (attempt) => 1000 * attempt,
  });
  const conversationsEnabled = !conversationsError;
  const effectiveConversationId = conversationsEnabled ? conversationId : null;

  const versionId = versionIdParam || (project?.maps && project.maps.length > 0 ? project.maps[project.maps.length - 1] : null);

  // When we need to trigger a refresh
  const invalidateMapData = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['project', projectId, 'map', versionId] });
  }, [queryClient, projectId, versionId]);

  // Function to update project data (invalidate project queries)
  const invalidateProjectData = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['project', projectId] });
  }, [queryClient, projectId]);

  const { error, data: mapData } = useQuery({
    queryKey: ['project', projectId, 'map', versionId],
    queryFn: async () => {
      const res = await apiFetch(`/api/maps/${versionId}`);
      if (res.status === 404) {
        throw new Error('Map not found');
      }
      return await res.json();
    },
    // prevent map (query parameter) refreshing this
    refetchOnMount: false,
    enabled: isReady && !!versionId,
  });

  const { data: mapTree } = useQuery({
    queryKey: ['project', projectId, 'map', versionId, 'tree', effectiveConversationId],
    queryFn: async () => {
      const res = await apiFetch(
        `/api/maps/${versionId}/tree${effectiveConversationId ? `?conversation_id=${effectiveConversationId}` : ''}`,
      );
      if (!res.ok) throw new Error('Failed to fetch map tree');
      return (await res.json()) as MapTreeResponse;
    },
    enabled: isReady && !!versionId,
    retry: 5,
    retryDelay: (attempt) => 1000 * attempt,
    placeholderData: (previousData) => {
      if (!previousData) return undefined;
      // mapTree being null/undefined makes the version visualization flicker, so
      // delete the conversation-related stuff from the tree, and use that as our
      // placeholder
      return {
        ...previousData,
        tree: previousData.tree.map((node) => ({
          ...node,
          messages: [], // conversation messages
        })),
      };
    },
  });

  // tracking ephemeral state, where reloading the page will reset
  const [errors, setErrors] = useState<ErrorEntry[]>([]);
  const [activeActions, setActiveActions] = useState<EphemeralAction[]>([]);
  const [streamingText, setStreamingText] = useState<string>('');
  const streamingTurnId = useRef<string | null>(null);
  const [zoomHistory, setZoomHistory] = useState<Array<{ bounds: [number, number, number, number] }>>([]);
  const [zoomHistoryIndex, setZoomHistoryIndex] = useState(-1);
  const mapRef = useRef<MLMap | null>(null);
  const processedBoundsActionIds = useRef<Set<string>>(new Set());
  const [, setEphemeralTileLayers] = useState<TileLayerUpdate[]>([]);

  // Helper function to add a new error
  const addError = useCallback((message: string, shouldOverrideMessages: boolean = false, sourceId?: string) => {
    setErrors((prevErrors) => {
      // if it already exists, bail out
      if (prevErrors.some((err) => err.message === message)) return prevErrors;

      const newError: ErrorEntry = {
        id: Date.now().toString() + Math.random().toString(36).substr(2, 9),
        message,
        timestamp: new Date(),
        shouldOverrideMessages,
        sourceId,
      };

      console.error(message);
      if (!shouldOverrideMessages) toast.error(message);

      // schedule the auto-dismiss
      setTimeout(() => {
        setErrors((current) => current.filter((e) => e.id !== newError.id));
      }, 30000);

      return [...prevErrors, newError];
    });
  }, []);

  // Helper function to dismiss a specific error
  const dismissError = useCallback((errorId: string) => {
    setErrors((prevErrors) => prevErrors.filter((error) => error.id !== errorId));
  }, []);

  const allowedExtensions = useMemo(() => {
    const exts: string[] = [];
    for (const key in DROPZONE_ACCEPT) {
      const arr = DROPZONE_ACCEPT[key as keyof typeof DROPZONE_ACCEPT];
      if (Array.isArray(arr)) exts.push(...arr);
    }
    return Array.from(new Set(exts));
  }, []);

  // Add state for tracking uploading files
  const [uploadingFiles, setUploadingFiles] = useState<UploadingFile[]>([]);

  // WebSocket using react-use-websocket
  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';

  // Track whether Clerk auth was ever active (to distinguish "no auth" from "auth died")
  const hadClerkAuth = useRef(false);
  // Track whether initial JWT resolution is complete (blocks WS until resolved)
  const [authResolved, setAuthResolved] = useState(false);

  // Resolve auth mode once on mount so we know whether to connect
  useEffect(() => {
    let mounted = true;
    getJwt().then((token: string | undefined) => {
      if (!mounted) return;
      if (token) hadClerkAuth.current = true;
      setAuthResolved(true);
    });
    return () => {
      mounted = false;
    };
  }, []);

  // Async URL factory: fetches a fresh JWT on every connect/reconnect.
  // react-use-websocket calls this function each time it opens a new connection,
  // so the token is always fresh (Clerk JWTs expire in 60s).
  const wsUrl = useMemo(() => {
    if (!conversationId) return null;
    if (!authResolved) return null; // block until initial auth check completes

    const baseUrl = `${wsProtocol}//${window.location.host}/api/maps/ws/${conversationId}/messages/updates`;

    if (!isAuthConfigured()) {
      // No Clerk key at all: legacy/no-auth mode, connect without token
      return baseUrl;
    }

    if (!hadClerkAuth.current) {
      // Clerk is configured but session is expired/absent on page load.
      // Don't connect without auth, it'll just get 403.
      return null;
    }

    // Return an async function so react-use-websocket fetches a fresh JWT
    // on each connection attempt (initial + every reconnect).
    // skipCache: true forces Clerk to issue a fresh token, avoiding the race
    // where tab-return reconnect grabs a stale cached token before the
    // TokenManager visibility handler has finished refreshing.
    return async () => {
      const token = await getJwt({ skipCache: true });
      if (!token) {
        // Token gone = session died. Throw to prevent connection with no auth.
        throw new Error('Session expired');
      }
      return `${baseUrl}?token=${token}`;
    };
  }, [conversationId, wsProtocol, authResolved]);

  // Track page visibility and allow socket to remain open for 10 minutes after hidden
  const WS_REMAIN_OPEN_FOR_MS = 10 * 60 * 1000; // 10 minutes
  const [isTabVisible, setIsTabVisible] = useState<boolean>(document.visibilityState === 'visible');
  const [hiddenTimeoutExpired, setHiddenTimeoutExpired] = useState<boolean>(false);
  const hiddenTimerRef = useRef<number | null>(null);

  useEffect(() => {
    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        setIsTabVisible(true);
        setHiddenTimeoutExpired(false);
        if (hiddenTimerRef.current !== null) {
          clearTimeout(hiddenTimerRef.current);
          hiddenTimerRef.current = null;
        }
      } else {
        setIsTabVisible(false);
        hiddenTimerRef.current = window.setTimeout(() => {
          setHiddenTimeoutExpired(true);
          hiddenTimerRef.current = null;
        }, WS_REMAIN_OPEN_FOR_MS);
      }
    };
    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange);
      if (hiddenTimerRef.current !== null) {
        clearTimeout(hiddenTimerRef.current);
      }
    };
  }, []);

  // WebSocket using react-use-websocket - only connect when in a conversation
  const shouldConnect = conversationId !== null && (isTabVisible || !hiddenTimeoutExpired);
  const backoffMs = [30, 1_000, 5_000, 15_000, 50_000];
  const { lastMessage, readyState } = useWebSocket(
    wsUrl,
    {
      onError: () => {
        // Check if auth is configured but we have no token. This catches both:
        // 1. Session expired mid-use (hadClerkAuth was true, token gone)
        // 2. Session already expired on page load (hadClerkAuth never became true)
        if (isAuthConfigured() && !getCachedToken()) {
          toast.error('Session expired. Please sign in again.', {
            action: { label: 'Sign in', onClick: () => window.location.reload() },
            duration: 10_000,
          });
        } else {
          toast.error('Chat connection error.');
        }
      },
      shouldReconnect: () => {
        // Don't retry if auth is configured but there's no token. Retrying
        // without auth just hammers the server with 403s.
        if (isAuthConfigured() && !getCachedToken()) return false;
        return true;
      },
      reconnectAttempts: 2880, // 24 hours of continuous work, at 30 seconds each = 2,880
      reconnectInterval: (attempt) => backoffMs[Math.min(attempt, backoffMs.length - 1)],
    },
    shouldConnect,
  );

  // Process incoming messages
  useEffect(() => {
    if (lastMessage) {
      try {
        const update: any = JSON.parse(lastMessage.data);

        // Check if this is a satellite imagery update (global broadcast)
        if (update && typeof update === 'object' && update.type === 'satellite_update') {
          // Bust cache on all satellite tile sources by appending a timestamp
          const map = mapRef.current;
          if (map) {
            const style = map.getStyle();
            if (style?.sources) {
              for (const [sourceId, source] of Object.entries(style.sources)) {
                if (sourceId.startsWith('satellite-source-') && source.type === 'raster') {
                  const rasterSource = map.getSource(sourceId);
                  if (rasterSource && 'setTiles' in rasterSource) {
                    const tiles = (source as any).tiles as string[] | undefined;
                    if (tiles?.length) {
                      const bustParam = `_t=${Date.now()}`;
                      const newTiles = tiles.map((t: string) => {
                        const sep = t.includes('?') ? '&' : '?';
                        // Remove any existing _t param
                        const cleaned = t.replace(/[&?]_t=\d+/, '');
                        return `${cleaned}${sep}${bustParam}`;
                      });
                      (rasterSource as any).setTiles(newTiles);
                    }
                  }
                }
              }
            }
          }
          toast.success('New satellite imagery available — map updated');
          return;
        }

        // Handle streaming tokens from Sage
        if (update && typeof update === 'object' && 'streaming' in update && update.streaming === true) {
          if (update.done) {
            streamingTurnId.current = null;
            setStreamingText('');
          } else if (update.token) {
            if (update.turn_id) streamingTurnId.current = update.turn_id;
            setStreamingText((prev) => prev + update.token);
          }
          return;
        }

        // Check if this is an ephemeral action
        if (update && typeof update === 'object' && 'ephemeral' in update && update.ephemeral === true) {
          const action = update as EphemeralAction;

          // Check if this is an error notification
          if (action.error_message) {
            streamingTurnId.current = null;
            setStreamingText('');
            addError(action.error_message, true);
            return;
          }

          // Handle bounds zooming only when action becomes active (not on completion)
          if (action.bounds && action.bounds.length === 4 && mapRef.current && action.status === 'active') {
            // Check if we've already processed this action
            if (processedBoundsActionIds.current.has(action.action_id)) {
              return;
            }
            processedBoundsActionIds.current.add(action.action_id);
            // Save current bounds to history before zooming
            const currentBounds = mapRef.current.getBounds();
            const currentBoundsArray: [number, number, number, number] = [
              currentBounds.getWest(),
              currentBounds.getSouth(),
              currentBounds.getEast(),
              currentBounds.getNorth(),
            ];

            // Add both current bounds and new bounds to history in a single update
            setZoomHistory((prev) => {
              const historyUpToCurrent = prev.slice(0, zoomHistoryIndex + 1);
              return [...historyUpToCurrent, { bounds: currentBoundsArray }, { bounds: action.bounds as [number, number, number, number] }];
            });

            // Update index to point to the final new bounds (current + 2 positions)
            setZoomHistoryIndex((prev) => prev + 2);

            // Zoom to new bounds
            const [west, south, east, north] = action.bounds;
            mapRef.current.fitBounds(
              [
                [west, south],
                [east, north],
              ],
              { animate: true, padding: 50 },
            );
          }

          if (action.updates?.add_tile_layer && mapRef.current) {
            const tl = action.updates.add_tile_layer;
            const map = mapRef.current;
            if (!map.getSource(tl.source_id)) {
              map.addSource(tl.source_id, {
                type: 'raster',
                tiles: tl.tiles,
                tileSize: tl.tileSize || 256,
                maxzoom: tl.maxzoom || 14,
                bounds: tl.bounds,
              });
              map.addLayer({
                id: tl.source_id,
                type: 'raster',
                source: tl.source_id,
                paint: { 'raster-opacity': 0.85 },
              });
              setEphemeralTileLayers((prev) => {
                if (prev.some((l) => l.source_id === tl.source_id)) return prev;
                return [...prev, tl];
              });
            }
          }

          if (action.status === 'active') {
            // Add to active actions
            setActiveActions((prev) => [...prev, action]);
          } else if (action.status === 'completed') {
            // Remove from active actions
            setActiveActions((prev) => prev.filter((a) => a.action_id !== action.action_id));

            if (action.updates?.style_json) {
              invalidateMapData();
              // Also invalidate the style query directly so MapLibre picks up
              // new layer styles even if the monotonic counter hasn't changed yet.
              queryClient.invalidateQueries({ queryKey: ['mapStyle'] });
            }
          }
        } else {
          // Non-ephemeral messages are of type SanitizedMessage.
          // Only clear streaming text if no active streaming turn is in progress,
          // preventing a race where PG NOTIFY delivers the saved message before
          // the Redis Pub/Sub streaming-done token arrives.
          if (!streamingTurnId.current) {
            setStreamingText('');
          }
          invalidateMapData();
        }
      } catch (e) {
        console.error('Error processing WebSocket message:', e);
        addError('Failed to process update from server.', false);
      }
    }
  }, [lastMessage, addError, zoomHistoryIndex, invalidateMapData, queryClient]);

  const MULTIPART_THRESHOLD = 50 * 1024 * 1024; // 50 MB
  // Browsers cap parallel HTTP requests to one origin at 6. Going higher just
  // queues the extras. 6 is the right ceiling for s3.gis.nozalabs.rw.
  const MULTIPART_CONCURRENCY = 6;
  const RESUME_TTL_MS = 7 * 24 * 60 * 60 * 1000;

  type MultipartResumeState = {
    upload_id: string;
    s3_key: string;
    layer_id: string;
    dag_child_map_id: string;
    dag_parent_map_id: string;
    total_parts: number;
    part_size: number;
    completed_parts: { part_number: number; etag: string }[];
    filename: string;
    file_size: number;
    created_at: number;
  };

  // Stable per-file fingerprint. Hashes filename + size + lastModified + first
  // 1MB of content. Skips hashing the whole file (3 GB → minutes); first 1MB
  // + metadata is unique enough to detect "same file, same machine, same upload".
  const computeFileFingerprint = async (file: File): Promise<string> => {
    const headBuf = await file.slice(0, 1024 * 1024).arrayBuffer();
    const enc = new TextEncoder();
    const metaBuf = enc.encode(`${file.name}|${file.size}|${file.lastModified}`);
    const combined = new Uint8Array(metaBuf.length + headBuf.byteLength);
    combined.set(metaBuf, 0);
    combined.set(new Uint8Array(headBuf), metaBuf.length);
    const hashBuf = await crypto.subtle.digest('SHA-1', combined);
    return Array.from(new Uint8Array(hashBuf))
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('');
  };

  const resumeKey = (pid: string, fp: string) => `mundi-multipart-resume-${pid}-${fp}`;

  const loadResumeState = (pid: string, fp: string): MultipartResumeState | null => {
    try {
      const raw = localStorage.getItem(resumeKey(pid, fp));
      if (!raw) return null;
      const state = JSON.parse(raw) as MultipartResumeState;
      if (Date.now() - state.created_at > RESUME_TTL_MS) {
        localStorage.removeItem(resumeKey(pid, fp));
        return null;
      }
      return state;
    } catch {
      return null;
    }
  };

  const saveResumeState = (pid: string, fp: string, state: MultipartResumeState): void => {
    try {
      localStorage.setItem(resumeKey(pid, fp), JSON.stringify(state));
    } catch {
      // localStorage full or disabled - non-fatal
    }
  };

  const clearResumeState = (pid: string, fp: string): void => {
    try {
      localStorage.removeItem(resumeKey(pid, fp));
    } catch {
      // ignore
    }
  };

  const uploadFile = useMutation({
    retry: false,
    mutationFn: async ({ file, fileId }: { file: File; fileId: string }): Promise<{ name: string; dag_child_map_id?: string }> => {
      if (!versionId) throw new Error('No version ID available');

      const useMultipart = file.size >= MULTIPART_THRESHOLD;

      if (useMultipart) {
        // --- Multipart upload: parallel chunks for large files ---
        // Send the file as-is. No client-side compression, no client-side
        // transformation. Browser only reads the file off disk and sends bytes.
        // All compute (decompression, COG generation, processing) is on the
        // server where it belongs.
        const uploadFilename = file.name;
        const payload: Blob = file;
        const payloadSize = payload.size;

        // Step 1: try to resume an interrupted prior upload of this same file.
        const fingerprint = await computeFileFingerprint(file);
        const saved = loadResumeState(projectId, fingerprint);
        type InitShape = {
          upload_id: string;
          s3_key: string;
          layer_id: string;
          part_size: number;
          total_parts: number;
          dag_child_map_id: string;
          dag_parent_map_id: string;
        };
        let init: InitShape | null = null;
        const completedParts: { part_number: number; etag: string }[] = [];

        if (saved) {
          // Cross-reference localStorage with S3 (S3 is the source of truth).
          // Saved upload_id may be expired / aborted; in that case start fresh.
          const statusRes = await fetchMaybeAuth(
            `/api/maps/${saved.dag_child_map_id}/upload-multipart-status?upload_id=${encodeURIComponent(saved.upload_id)}&s3_key=${encodeURIComponent(saved.s3_key)}`,
          );
          if (statusRes.ok) {
            const status = (await statusRes.json()) as {
              exists: boolean;
              parts: { part_number: number; etag: string }[];
            };
            if (status.exists && status.parts.length > 0) {
              const s3Set = new Set(status.parts.map((p) => p.part_number));
              const confirmed = saved.completed_parts.filter((p) => s3Set.has(p.part_number));
              const pct = Math.round((confirmed.length / saved.total_parts) * 100);
              const ok = window.confirm(
                `Resume previous upload of "${saved.filename}"?\n\n` +
                  `${confirmed.length} of ${saved.total_parts} chunks already on the server (~${pct}%).\n\n` +
                  `OK = resume from where it left off.\n` +
                  `Cancel = start over from 0%.`,
              );
              if (ok) {
                completedParts.push(...confirmed);
                init = {
                  upload_id: saved.upload_id,
                  s3_key: saved.s3_key,
                  layer_id: saved.layer_id,
                  part_size: saved.part_size,
                  total_parts: saved.total_parts,
                  dag_child_map_id: saved.dag_child_map_id,
                  dag_parent_map_id: saved.dag_parent_map_id,
                };
              }
            }
          }
          if (!init) clearResumeState(projectId, fingerprint);
        }

        if (!init) {
          // Fresh init (no saved state, expired, or resume declined).
          const initRes = await fetchMaybeAuth(
            `/api/maps/${versionId}/upload-multipart-init?filename=${encodeURIComponent(uploadFilename)}&file_size=${payloadSize}`,
            { method: 'POST', headers: { 'Content-Type': 'application/json' } },
          );
          if (!initRes.ok) {
            const err = await initRes.json().catch(() => ({ detail: initRes.statusText }));
            throw new Error(typeof err.detail === 'string' ? err.detail : 'Failed to init multipart upload');
          }
          init = (await initRes.json()) as InitShape;
        }

        const partSize = init.part_size;
        const totalParts = init.total_parts;
        // Persist initial resume state so a crash mid-upload is recoverable.
        const persistResume = () => {
          saveResumeState(projectId, fingerprint, {
            upload_id: init.upload_id,
            s3_key: init.s3_key,
            layer_id: init.layer_id,
            dag_child_map_id: init.dag_child_map_id,
            dag_parent_map_id: init.dag_parent_map_id,
            total_parts: init.total_parts,
            part_size: init.part_size,
            completed_parts: [...completedParts],
            filename: file.name,
            file_size: file.size,
            created_at: Date.now(),
          });
        };
        persistResume();

        // Track bytes-in-flight per part so the bar reflects ALL streams,
        // not just completed parts. High-water mark prevents the bar from
        // ever moving backward when a part fails and resets to 0.
        const inFlightBytes = new Map<number, number>();
        // Resumed parts count as already-completed bytes. Last part may be smaller.
        let completedBytes = Math.min(payloadSize, completedParts.length * partSize);
        let highWaterPercent = 0;

        // XHR progress events fire 30-60 times/sec across 6 streams. Re-rendering
        // React on every event burns the laptop's CPU for no visual benefit.
        // Only update state when the displayed percentage actually changes.
        let lastDisplayedPercent = -1;
        const updateProgress = () => {
          let inFlight = 0;
          for (const v of inFlightBytes.values()) inFlight += v;
          const totalUploaded = completedBytes + inFlight;
          const raw = Math.min(90, Math.round((totalUploaded / payloadSize) * 90));
          if (raw > highWaterPercent) highWaterPercent = raw;
          if (highWaterPercent === lastDisplayedPercent) return;
          lastDisplayedPercent = highWaterPercent;
          setUploadingFiles((prev) => prev.map((f) => (f.id === fileId ? { ...f, progress: highWaterPercent } : f)));
        };

        const putPartOnce = (partUrl: string, blob: Blob, partNum: number): Promise<XMLHttpRequest> =>
          new Promise<XMLHttpRequest>((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            // 15 min per 50MB chunk → tolerates ~55 KB/s sustained on a single
            // stream. With 6 parallel streams, overall throughput floor ~330 KB/s.
            xhr.timeout = 15 * 60 * 1000;
            xhr.upload.addEventListener('progress', (ev) => {
              if (ev.lengthComputable) {
                inFlightBytes.set(partNum, ev.loaded);
                updateProgress();
              }
            });
            xhr.addEventListener('load', () => {
              if (xhr.status >= 200 && xhr.status < 300) resolve(xhr);
              else reject(new Error(`HTTP ${xhr.status}`));
            });
            xhr.addEventListener('error', () => reject(new Error('network error')));
            xhr.addEventListener('timeout', () => reject(new Error('timeout')));
            xhr.addEventListener('abort', () => reject(new Error('aborted')));
            xhr.open('PUT', partUrl);
            xhr.send(blob);
          });

        const uploadPart = async (partNum: number): Promise<void> => {
          const start = (partNum - 1) * partSize;
          const end = Math.min(start + partSize, payloadSize);
          const blob = payload.slice(start, end);

          // Up to 4 attempts per part with exponential backoff (1s, 2s, 4s).
          let lastErr: unknown = null;
          for (let attempt = 0; attempt < 4; attempt++) {
            try {
              // Re-presign on each attempt (URL expires in 1h, attempt may be late).
              const presignRes = await fetchMaybeAuth(`/api/maps/${init.dag_child_map_id}/upload-multipart-presign`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ s3_key: init.s3_key, upload_id: init.upload_id, part_numbers: [partNum] }),
              });
              if (!presignRes.ok) throw new Error(`presign HTTP ${presignRes.status}`);
              const { urls } = (await presignRes.json()) as { urls: Record<number, string> };

              inFlightBytes.set(partNum, 0);
              const resp = await putPartOnce(urls[partNum], blob, partNum);
              const etag = (resp.getResponseHeader('ETag') || '').replace(/"/g, '');
              completedParts.push({ part_number: partNum, etag });
              inFlightBytes.delete(partNum);
              completedBytes += end - start;
              updateProgress();
              // Persist after each successful part so a crash leaves us
              // recoverable from this exact point.
              persistResume();
              return;
            } catch (e) {
              lastErr = e;
              inFlightBytes.delete(partNum);
              if (attempt < 3) {
                await new Promise((r) => setTimeout(r, 1000 * 2 ** attempt));
              }
            }
          }
          throw new Error(`Part ${partNum} failed after 4 attempts: ${(lastErr as Error)?.message ?? 'unknown'}`);
        };

        // Workers pull from a shared queue → true bounded concurrency.
        // Skip parts already confirmed by S3 (resumed uploads).
        const completedSet = new Set(completedParts.map((p) => p.part_number));
        const queue = Array.from({ length: totalParts }, (_, i) => i + 1).filter((pn) => !completedSet.has(pn));
        // Initial paint: show resumed % immediately so user sees "Resumed at 67%"
        // not "0% then jumps".
        updateProgress();
        const worker = async (): Promise<void> => {
          while (queue.length > 0) {
            const pn = queue.shift();
            if (pn === undefined) return;
            await uploadPart(pn);
          }
        };
        const workerCount = Math.min(MULTIPART_CONCURRENCY, queue.length || 1);
        await Promise.all(Array.from({ length: workerCount }, () => worker()));

        setUploadingFiles((prev) => prev.map((f) => (f.id === fileId ? { ...f, progress: 92 } : f)));

        // Complete multipart upload (assembles parts in S3). Send the upload
        // filename — backend strips .gz and decompresses if needed.
        // Long timeout: S3 has to assemble all parts. For 60+ parts on a
        // 3 GB file this can take a couple of minutes. Default 30s aborts.
        const assembleRes = await fetchMaybeAuth(`/api/maps/${init.dag_child_map_id}/upload-multipart-complete`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          signal: AbortSignal.timeout(10 * 60 * 1000),
          body: JSON.stringify({
            s3_key: init.s3_key,
            upload_id: init.upload_id,
            parts: completedParts,
            layer_id: init.layer_id,
            filename: uploadFilename,
            add_layer_to_map: true,
          }),
        });
        if (!assembleRes.ok) {
          const err = await assembleRes.json().catch(() => ({ detail: assembleRes.statusText }));
          throw new Error(typeof err.detail === 'string' ? err.detail : 'Failed to assemble multipart upload');
        }

        setUploadingFiles((prev) => prev.map((f) => (f.id === fileId ? { ...f, progress: 95 } : f)));

        // Process the uploaded file (same as single-PUT flow). Backend
        // strips .gz from filename and decompresses before processing.
        // Long timeout: backend downloads from S3, runs preprocessing
        // (gunzip if compressed, COG generation, raster reprojection, etc).
        // For a 3 GB file this is 5-15 minutes. Default 30s aborts.
        const completeRes = await fetchMaybeAuth(`/api/maps/${init.dag_child_map_id}/upload-complete`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          signal: AbortSignal.timeout(60 * 60 * 1000),
          body: JSON.stringify({
            s3_key: init.s3_key,
            layer_id: init.layer_id,
            filename: uploadFilename,
            add_layer_to_map: true,
          }),
        });
        if (!completeRes.ok) {
          const err = await completeRes.json().catch(() => ({ detail: completeRes.statusText }));
          throw new Error(typeof err.detail === 'string' ? err.detail : 'Processing failed after upload');
        }

        // Successful round trip: resume state is no longer needed.
        clearResumeState(projectId, fingerprint);
        return await completeRes.json();
      }

      // --- Single PUT for small files (< 50 MB) ---
      const presignRes = await fetchMaybeAuth(`/api/maps/${versionId}/upload-presign?filename=${encodeURIComponent(file.name)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      });
      if (!presignRes.ok) {
        const err = await presignRes.json().catch(() => ({ detail: presignRes.statusText }));
        const d = err.detail;
        throw new Error(typeof d === 'string' ? d : d ? JSON.stringify(d) : 'Failed to get upload URL');
      }
      const presign = (await presignRes.json()) as {
        upload_url: string;
        s3_key: string;
        layer_id: string;
        dag_child_map_id: string;
        dag_parent_map_id: string;
      };

      await new Promise<void>((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.upload.addEventListener('progress', (event) => {
          if (event.lengthComputable) {
            const progress = Math.round((event.loaded / event.total) * 95);
            setUploadingFiles((prev) => prev.map((f) => (f.id === fileId ? { ...f, progress } : f)));
          }
        });
        xhr.addEventListener('load', () => {
          if (xhr.status >= 200 && xhr.status < 300) resolve();
          else reject(new Error(`S3 upload failed (HTTP ${xhr.status})`));
        });
        xhr.addEventListener('error', () => reject(new Error('Upload failed due to network error')));
        xhr.open('PUT', presign.upload_url);
        xhr.send(file);
      });

      setUploadingFiles((prev) => prev.map((f) => (f.id === fileId ? { ...f, progress: 97 } : f)));

      const completeRes = await fetchMaybeAuth(`/api/maps/${presign.dag_child_map_id}/upload-complete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          s3_key: presign.s3_key,
          layer_id: presign.layer_id,
          filename: file.name,
          add_layer_to_map: true,
        }),
      });
      if (!completeRes.ok) {
        const err = await completeRes.json().catch(() => ({ detail: completeRes.statusText }));
        const d2 = err.detail;
        throw new Error(typeof d2 === 'string' ? d2 : d2 ? JSON.stringify(d2) : 'Processing failed after upload');
      }

      return await completeRes.json();
    },
    onSuccess: (response, { fileId }) => {
      toast.success(`Layer "${response.name}" uploaded successfully! Navigating to new map...`);

      // Mark as completed
      setUploadingFiles((prev) => prev.map((f) => (f.id === fileId ? { ...f, status: 'completed', progress: 100 } : f)));

      // Remove from uploading list after delay
      setTimeout(() => {
        setUploadingFiles((prev) => prev.filter((f) => f.id !== fileId));
      }, 2000);

      // Invalidate project data to refresh the project state
      queryClient.invalidateQueries({ queryKey: ['project', projectId] });

      // Navigate to the new child map if dag_child_map_id is present
      if (response.dag_child_map_id) {
        setTimeout(() => {
          navigate(`/project/${projectId}/${response.dag_child_map_id}`);
        }, 1000);
      } else {
        // Fallback: refresh the current map data
        setTimeout(() => {
          invalidateMapData();
        }, 2000);
      }
    },
    onError: (error, { file, fileId }) => {
      const errorMessage = error instanceof Error ? error.message : 'Unknown error';
      setUploadingFiles((prev) => prev.map((f) => (f.id === fileId ? { ...f, status: 'error', error: errorMessage } : f)));
      toast.error(`Error uploading ${file.name}: ${errorMessage}`);

      // Remove from uploading list after delay to show error state
      setTimeout(() => {
        setUploadingFiles((prev) => prev.filter((f) => f.id !== fileId));
      }, 5000);
    },
  });

  // Modified dropzone implementation to handle multiple files
  const onDrop = useCallback(
    (acceptedFiles: File[]) => {
      if (!versionId || acceptedFiles.length === 0) return;

      const maxFileSize = 5 * 1024 * 1024 * 1024; // 5GB in bytes

      // Filter out files that are too large
      const validFiles = acceptedFiles.filter((file) => {
        if (file.size > maxFileSize) {
          toast.error(`File "${file.name}" is too large. Files over 5GB aren't supported yet.`);
          return false;
        }
        return true;
      });

      if (validFiles.length === 0) return;

      // Create uploading file entries
      const newUploadingFiles: UploadingFile[] = validFiles.map((file) => ({
        id: `${file.name}-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`,
        file,
        progress: 0,
        status: 'uploading',
      }));

      // Add to uploading files state
      setUploadingFiles((prev) => [...prev, ...newUploadingFiles]);

      // Start uploading each file
      newUploadingFiles.forEach((uploadingFile) => {
        uploadFile.mutate({ file: uploadingFile.file, fileId: uploadingFile.id });
      });
    },
    [versionId, uploadFile],
  );

  const { getRootProps, getInputProps, isDragActive, open } = useDropzone({
    onDrop,
    onDropRejected: (fileRejections) => {
      for (const rejection of fileRejections) {
        addError(`Cannot upload "${rejection.file.name}": Allowed extensions: ${allowedExtensions.join(', ')}`);
      }
    },
    noClick: true, // Prevent opening the file dialog when clicking
    accept: DROPZONE_ACCEPT,
  });

  // Let them hide certain layers client-side only
  const [hiddenLayerIDs, setHiddenLayerIDs] = useState<string[]>([]);
  const toggleLayerVisibility = (layerId: string) => {
    setHiddenLayerIDs((prev) => (prev.includes(layerId) ? prev.filter((id) => id !== layerId) : [...prev, layerId]));
  };

  // If Clerk has loaded and the user is definitively not signed in (session
  // expired or never signed in), show a sign-in prompt instead of an infinite spinner.
  if (isSignedOut) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-background">
        <div className="text-center max-w-md px-6">
          <h1 className="text-2xl font-bold mb-3">Sign in to continue</h1>
          <p className="text-muted-foreground mb-4">Your session has expired or you need to sign in to view this project.</p>
          <button
            type="button"
            className="inline-flex items-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
            onClick={() => {
              window.location.href = '/';
            }}
          >
            Sign in
          </button>
        </div>
      </div>
    );
  }

  if (!project || !versionId) {
    return (
      <div className="p-6">
        <h1 className="text-2xl font-bold mb-4">
          Loading project {projectId} version {versionId}...
        </h1>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-6">
        <h1 className="text-2xl font-bold mb-4">Error Loading Map</h1>
        <p>Failed to load map data: {error.message}</p>
        <a href="/maps" className="text-blue-500 hover:underline">
          Back to Maps
        </a>
      </div>
    );
  }

  return (
    <div {...getRootProps()} className={`flex grow ${isDragActive ? 'file-drag-active' : ''}`}>
      {/* Dropzone */}
      <input {...getInputProps()} />

      {/* Interactive Map Section */}
      <MapLibreMap
        mapId={versionId}
        height="100%"
        project={project}
        mapData={mapData}
        mapTree={mapTree || null}
        conversationId={effectiveConversationId}
        conversations={conversations || []}
        conversationsEnabled={conversationsEnabled}
        setConversationId={setConversationId}
        readyState={readyState}
        openDropzone={open}
        uploadingFiles={uploadingFiles}
        hiddenLayerIDs={hiddenLayerIDs}
        toggleLayerVisibility={toggleLayerVisibility}
        mapRef={mapRef}
        activeActions={activeActions}
        setActiveActions={setActiveActions}
        streamingText={streamingText}
        zoomHistory={zoomHistory}
        zoomHistoryIndex={zoomHistoryIndex}
        setZoomHistoryIndex={setZoomHistoryIndex}
        addError={addError}
        dismissError={dismissError}
        errors={errors}
        invalidateProjectData={invalidateProjectData}
        invalidateMapData={invalidateMapData}
      />
    </div>
  );
}
