import { cogProtocol } from '@geomatico/maplibre-cog-protocol';
import { ApiKeys } from '@mundi/ee';
import maplibregl from 'maplibre-gl';
import { Protocol } from 'pmtiles';
import { lazy, Suspense, useEffect, useState } from 'react';
import * as reactRouterDom from 'react-router-dom';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { AppSidebar } from '@/components/app-sidebar';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { SidebarProvider } from '@/components/ui/sidebar';
import { Toaster } from '@/components/ui/sonner';
import { ProjectsProvider } from './contexts/ProjectsContext';
import './App.css';
import { Routes as EERoutes, OptionalAuth, Provider, RequireAuth } from '@mundi/ee';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// Lazy-loaded route components — each gets its own chunk
const MapsList = lazy(() => import('./components/MapsList'));
const ProjectView = lazy(() => import('./components/ProjectView'));
const PostGISDocumentation = lazy(() => import('./pages/PostGISDocumentation'));
const RwandaDashboard = lazy(() => import('./components/RwandaDashboard').then((m) => ({ default: m.RwandaDashboard })));
const NotFound = lazy(() => import('./pages/NotFound'));
const PrivacyPolicy = lazy(() => import('./pages/PrivacyPolicy'));
const TermsOfService = lazy(() => import('./pages/TermsOfService'));

function RouteLoader() {
  return (
    <div className="flex items-center justify-center min-h-[50vh]">
      <div className="animate-spin h-8 w-8 border-4 border-primary border-t-transparent rounded-full" />
    </div>
  );
}

function AppContent() {
  useEffect(() => {
    const protocol = new Protocol();
    maplibregl.addProtocol('pmtiles', protocol.tile);
    maplibregl.addProtocol('cog', cogProtocol);
    return () => {
      maplibregl.removeProtocol('pmtiles');
      maplibregl.removeProtocol('cog');
    };
  }, []);

  return (
    <BrowserRouter>
      <SidebarProvider className="z-50">
        <ProjectsProvider>
          <AppSidebar />

          <ErrorBoundary>
            <Suspense fallback={<RouteLoader />}>
              <Routes>
                {EERoutes(reactRouterDom)}
                {/* App Routes */}
                <Route
                  path="/"
                  element={
                    <RequireAuth>
                      <MapsList />
                    </RequireAuth>
                  }
                />
                <Route
                  path="/project/:projectId/:versionIdParam?"
                  element={
                    <OptionalAuth>
                      <ProjectView />
                    </OptionalAuth>
                  }
                />
                <Route
                  path="/postgis/:connectionId"
                  element={
                    <RequireAuth>
                      <PostGISDocumentation />
                    </RequireAuth>
                  }
                />
                <Route
                  path="/settings/api-keys"
                  element={
                    <Suspense fallback={<RouteLoader />}>
                      <RequireAuth>
                        <ApiKeys />
                      </RequireAuth>
                    </Suspense>
                  }
                />
                <Route
                  path="/rwanda"
                  element={
                    <OptionalAuth>
                      <RwandaDashboard />
                    </OptionalAuth>
                  }
                />

                <Route path="/privacy" element={<PrivacyPolicy />} />
                <Route path="/terms" element={<TermsOfService />} />
                <Route path="*" element={<NotFound />} />
              </Routes>
            </Suspense>
          </ErrorBoundary>
        </ProjectsProvider>
      </SidebarProvider>
    </BrowserRouter>
  );
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 3,
      retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 15000),
      staleTime: 30_000, // 30s — data stays fresh for 30s before background refetch
      gcTime: 5 * 60_000, // 5min — unused cache entries garbage-collected after 5min
      networkMode: 'online', // pause queries when browser goes offline
    },
    mutations: {
      retry: 1,
      networkMode: 'online',
    },
  },
});

function OfflineBanner() {
  const [isOffline, setIsOffline] = useState(!navigator.onLine);
  useEffect(() => {
    const goOffline = () => setIsOffline(true);
    const goOnline = () => setIsOffline(false);
    window.addEventListener('offline', goOffline);
    window.addEventListener('online', goOnline);
    return () => {
      window.removeEventListener('offline', goOffline);
      window.removeEventListener('online', goOnline);
    };
  }, []);
  if (!isOffline) return null;
  return (
    <div className="fixed top-0 left-0 right-0 z-[9999] bg-destructive text-destructive-foreground text-center py-2 text-sm font-medium">
      You are offline. Some features may be unavailable.
    </div>
  );
}

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Provider>
        <OfflineBanner />
        <AppContent />
        <Toaster />
      </Provider>
    </QueryClientProvider>
  );
}

export default App;
