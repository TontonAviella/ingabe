import { apiFetch, useIsReady } from '@mundi/ee';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { createContext, ReactNode, useContext, useState } from 'react';
import { useIndustry } from '@/components/IndustryGate';
import { track, trackDuration, trackError } from '../lib/analytics';
import { MapProject, ProjectList } from '../lib/types';

interface ProjectsContextValue {
  // Pagination state
  currentPage: number;
  showDeleted: boolean;
  setCurrentPage: (page: number) => void;
  setShowDeleted: (show: boolean) => void;

  // Data and loading states
  projects: MapProject[];
  totalPages: number;
  totalItems: number;
  // The industry the list shows (the server's choice: the one the user works in now); null: every industry.
  industryLabel: string | null;
  otherIndustries: ProjectList['other_industries'];
  isLoading: boolean;
  error: string | null;

  // Actions
  createProject: () => Promise<void>;
  deleteProject: (projectId: string) => Promise<void>;
  refetchProjects: () => void;

  // All projects for sidebar (recent projects)
  allProjects: MapProject[];
  allProjectsLoading: boolean;
}

const ProjectsContext = createContext<ProjectsContextValue | undefined>(undefined);

interface ProjectsProviderProps {
  children: ReactNode;
}

export function ProjectsProvider({ children }: ProjectsProviderProps) {
  const [showDeleted, setShowDeleted] = useState(false);
  const queryClient = useQueryClient();
  const isReady = useIsReady();
  // The server lists the projects of the industry the user works in now. The lists wait until that industry is
  // known and reload when the picker saves another one (it replaces the industry query's data).
  const industryQuery = useIndustry();
  const industry = industryQuery.data?.industry ?? null;
  const waitingForIndustry = isReady && industryQuery.isPending;
  const listsEnabled = isReady && !industryQuery.isPending;
  // The page belongs to the industry it was chosen in: after a switch the list starts at page 1 in the same render,
  // so page N of the new industry is never requested.
  const [paging, setPaging] = useState<{ industry: string | null; page: number }>({ industry, page: 1 });
  const currentPage = paging.industry === industry ? paging.page : 1;
  const setCurrentPage = (page: number) => setPaging({ industry, page });

  // Query for paginated projects (main list)
  const {
    data: paginatedData,
    isLoading,
    error: queryError,
    refetch: refetchProjects,
  } = useQuery<ProjectList>({
    queryKey: ['projects', industry, currentPage, showDeleted],
    enabled: listsEnabled,
    queryFn: async () => {
      const response = await apiFetch(`/api/projects/?page=${currentPage}&limit=12&include_deleted=${showDeleted}`);
      if (!response.ok) {
        const error = new Error(`Failed to fetch projects: ${response.status} ${response.statusText}`);
        trackError('project_list_fetch_failed', error, {
          http_status: response.status,
          page: currentPage,
          include_deleted: showDeleted,
        });
        throw error;
      }
      try {
        return await response.json();
      } catch (error) {
        trackError('project_list_parse_failed', error, {
          page: currentPage,
          include_deleted: showDeleted,
        });
        throw error;
      }
    },
  });

  // Query for all projects (for sidebar recent projects)
  const { data: allProjectsData, isLoading: allProjectsLoading } = useQuery<ProjectList>({
    queryKey: ['projects', 'all', industry],
    enabled: listsEnabled,
    queryFn: async () => {
      const response = await apiFetch('/api/projects/');
      if (!response.ok) {
        const error = new Error(`Failed to fetch all projects: ${response.status} ${response.statusText}`);
        trackError('project_sidebar_fetch_failed', error, {
          http_status: response.status,
        });
        throw error;
      }
      try {
        return await response.json();
      } catch (error) {
        trackError('project_sidebar_parse_failed', error);
        throw error;
      }
    },
  });

  // Mutation for creating projects
  const createProjectMutation = useMutation({
    mutationFn: async () => {
      const startedAt = Date.now();
      track('project_create_started');
      const response = await apiFetch('/api/maps/create', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          title: 'New Map',
          description: '',
          project: {
            layers: [],
          },
        }),
      });

      if (!response.ok) {
        const error = new Error(`Failed to create map: ${response.status} ${response.statusText}`);
        trackError('project_create_failed', error, { http_status: response.status });
        throw error;
      }

      const payload = await response.json();
      trackDuration('project_create_succeeded', startedAt, {
        project_id: typeof payload?.project_id === 'string' ? payload.project_id : undefined,
        map_id: typeof payload?.map_id === 'string' ? payload.map_id : undefined,
      });
      return payload;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });

  // Mutation for deleting projects
  const deleteProjectMutation = useMutation({
    mutationFn: async (projectId: string) => {
      const startedAt = Date.now();
      track('project_delete_started', { project_id: projectId });
      const response = await apiFetch(`/api/projects/${projectId}`, {
        method: 'DELETE',
      });

      if (!response.ok) {
        const error = new Error(`Failed to delete map: ${response.status} ${response.statusText}`);
        trackError('project_delete_failed', error, { project_id: projectId, http_status: response.status });
        throw error;
      }

      const payload = await response.json();
      trackDuration('project_delete_succeeded', startedAt, { project_id: projectId });
      return payload;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });

  const value: ProjectsContextValue = {
    currentPage,
    showDeleted,
    setCurrentPage,
    setShowDeleted: (show: boolean) => {
      setShowDeleted(show);
      setCurrentPage(1); // Reset to first page when changing filter
    },

    projects: paginatedData?.projects || [],
    totalPages: paginatedData?.total_pages || 1,
    totalItems: paginatedData?.total_items || 0,
    industryLabel: paginatedData?.industry_label ?? null,
    otherIndustries: paginatedData?.other_industries ?? [],
    isLoading: isLoading || waitingForIndustry,
    error: queryError instanceof Error ? queryError.message : null,

    createProject: async () => {
      await createProjectMutation.mutateAsync();
    },
    deleteProject: async (projectId: string) => {
      await deleteProjectMutation.mutateAsync(projectId);
    },
    refetchProjects: () => {
      refetchProjects();
    },

    allProjects: allProjectsData?.projects || [],
    allProjectsLoading: allProjectsLoading || waitingForIndustry,
  };

  return <ProjectsContext.Provider value={value}>{children}</ProjectsContext.Provider>;
}

export function useProjects() {
  const context = useContext(ProjectsContext);
  if (context === undefined) {
    throw new Error('useProjects must be used within a ProjectsProvider');
  }
  return context;
}
