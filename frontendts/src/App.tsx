import { cogProtocol } from '@geomatico/maplibre-cog-protocol';
import { ApiKeys } from '@mundi/ee';
import maplibregl from 'maplibre-gl';
import { Protocol } from 'pmtiles';
import { lazy, Suspense, useEffect } from 'react';
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
    },
  },
});

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Provider>
        <AppContent />
        <Toaster />
      </Provider>
    </QueryClientProvider>
  );
}

export default App;
