import { apiFetch, useIsReady } from '@mundi/ee';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { createContext, ReactNode, useContext, useState } from 'react';
import { MapProject } from '../lib/types';

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
  const [currentPage, setCurrentPage] = useState(1);
  const [showDeleted, setShowDeleted] = useState(false);
  const queryClient = useQueryClient();
  const isReady = useIsReady();

  // Query for paginated projects (main list)
  const {
    data: paginatedData,
    isLoading,
    error: queryError,
    refetch: refetchProjects,
  } = useQuery({
    queryKey: ['projects', currentPage, showDeleted],
    enabled: isReady,
    queryFn: async () => {
      const response = await apiFetch(`/api/projects/?page=${currentPage}&limit=12&include_deleted=${showDeleted}`);
      if (!response.ok) {
        throw new Error(`Failed to fetch projects: ${response.status} ${response.statusText}`);
      }
      return response.json();
    },
  });

  // Query for all projects (for sidebar recent projects)
  const { data: allProjectsData, isLoading: allProjectsLoading } = useQuery({
    queryKey: ['projects', 'all'],
    enabled: isReady,
    queryFn: async () => {
      const response = await apiFetch('/api/projects/');
      if (!response.ok) {
        throw new Error(`Failed to fetch all projects: ${response.status} ${response.statusText}`);
      }
      return response.json();
    },
  });

  // Mutation for creating projects
  const createProjectMutation = useMutation({
    mutationFn: async () => {
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
        throw new Error('Failed to create map');
      }

      return response.json();
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });

  // Mutation for deleting projects
  const deleteProjectMutation = useMutation({
    mutationFn: async (projectId: string) => {
      const response = await apiFetch(`/api/projects/${projectId}`, {
        method: 'DELETE',
      });

      if (!response.ok) {
        throw new Error('Failed to delete map');
      }

      return response.json();
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });

  const value: ProjectsContextValue = {
    currentPage,
    showDeleted,
    setCurrentPage: (page: number) => {
      setCurrentPage(page);
    },
    setShowDeleted: (show: boolean) => {
      setShowDeleted(show);
      setCurrentPage(1); // Reset to first page when changing filter
    },

    projects: paginatedData?.projects || [],
    totalPages: paginatedData?.total_pages || 1,
    totalItems: paginatedData?.total_items || 0,
    isLoading,
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
    allProjectsLoading,
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
