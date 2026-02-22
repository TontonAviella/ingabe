import { apiFetch } from '@mundi/ee';
import { Database, Loader2, RefreshCw, Trash2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import MermaidComponent from './MermaidComponent';

interface DatabaseDetailsDialogProps {
  isOpen: boolean;
  onClose: () => void;
  databaseName: string;
  connectionId: string;
  projectId: string;
  onDelete?: () => void;
}

const DatabaseDetailsDialog = ({ isOpen, onClose, databaseName, connectionId, projectId, onDelete }: DatabaseDetailsDialogProps) => {
  const [documentation, setDocumentation] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const [isRegenerating, setIsRegenerating] = useState(false);

  useEffect(() => {
    if (isOpen && connectionId && projectId) {
      setLoading(true);
      setError(null);

      apiFetch(`/api/projects/${projectId}/postgis-connections/${connectionId}/documentation`)
        .then((response) => {
          if (!response.ok) {
            throw new Error(`Failed to fetch documentation: ${response.statusText}`);
          }
          return response.json();
        })
        .then((data) => {
          setDocumentation(data.documentation);
        })
        .catch((err) => {
          console.error('Error fetching database documentation:', err);
          setError(err.message);
          setDocumentation(null);
        })
        .finally(() => {
          setLoading(false);
        });
    }
  }, [isOpen, connectionId, projectId]);

  const handleDelete = async () => {
    if (!connectionId || !projectId || !onDelete) return;

    setIsDeleting(true);
    try {
      const response = await apiFetch(`/api/projects/${projectId}/postgis-connections/${connectionId}`, {
        method: 'DELETE',
      });

      if (!response.ok) {
        throw new Error(`Failed to delete connection: ${response.statusText}`);
      }

      onDelete();
      onClose();
    } catch (err) {
      console.error('Error deleting database connection:', err);
      setError(err instanceof Error ? err.message : 'Failed to delete connection');
    } finally {
      setIsDeleting(false);
    }
  };

  const handleRegenerate = async () => {
    if (!connectionId || !projectId) return;

    setIsRegenerating(true);
    setError(null);

    try {
      const response = await apiFetch(`/api/projects/${projectId}/postgis-connections/${connectionId}/regenerate-documentation`, {
        method: 'POST',
      });

      if (!response.ok) {
        throw new Error(`Failed to regenerate documentation: ${response.statusText}`);
      }

      // Wait a moment and then refetch the documentation
      setTimeout(() => {
        setLoading(true);
        apiFetch(`/api/projects/${projectId}/postgis-connections/${connectionId}/documentation`)
          .then((response) => {
            if (!response.ok) {
              throw new Error(`Failed to fetch documentation: ${response.statusText}`);
            }
            return response.json();
          })
          .then((data) => {
            setDocumentation(data.documentation);
          })
          .catch((err) => {
            console.error('Error fetching database documentation:', err);
            setError(err.message);
            setDocumentation(null);
          })
          .finally(() => {
            setLoading(false);
            setIsRegenerating(false);
          });
      }, 2000); // Wait 2 seconds before refetching
    } catch (err) {
      console.error('Error regenerating database documentation:', err);
      setError(err instanceof Error ? err.message : 'Failed to regenerate documentation');
      setIsRegenerating(false);
    }
  };

  // Fallback content for when documentation is not available
  const fallbackContent = `
Documentation is being generated for this database. Please check back in a few moments.

If documentation generation fails, this indicates the database connection details or the database structure couldn't be analyzed automatically.
`;

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className="max-w-4xl max-h-[90vh] flex flex-col xl:!max-w-4xl">
        <DialogHeader>
          <DialogTitle className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Database className="h-5 w-5" />
              {databaseName}
            </div>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={handleRegenerate}
                disabled={isRegenerating || loading}
                className="cursor-pointer"
              >
                {isRegenerating ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                {isRegenerating ? 'Regenerating...' : 'Regenerate'}
              </Button>
              {onDelete && (
                <Button variant="destructive" size="sm" onClick={handleDelete} disabled={isDeleting} className="cursor-pointer mr-8">
                  {isDeleting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
                  {isDeleting ? 'Deleting...' : 'Delete'}
                </Button>
              )}
            </div>
          </DialogTitle>
        </DialogHeader>

        <div className="flex flex-col gap-6 flex-1 min-h-0">
          <div className="overflow-y-auto pr-4">
            <div className="space-y-6">
              {loading && (
                <div className="flex items-center justify-center p-8">
                  <Loader2 className="h-6 w-6 animate-spin mr-2" />
                  <span>Loading database documentation...</span>
                </div>
              )}

              {error && (
                <div className="p-4 border border-red-500 rounded-lg">
                  <p className="text-red-500">Error loading documentation: {error}</p>
                </div>
              )}

              {!loading && !error && (
                <div className="prose prose-sm prose-invert max-w-none">
                  <ReactMarkdown
                    components={{
                      code(props) {
                        const { className, children, ...rest } = props;
                        const match = /language-(\w+)/.exec(className || '');
                        const language = match ? match[1] : '';

                        if (language === 'mermaid') {
                          return (
                            <div className="bg-muted/20 mx-auto">
                              <MermaidComponent chart={String(children)} />
                            </div>
                          );
                        }

                        return (
                          <code className={className} {...rest}>
                            {children}
                          </code>
                        );
                      },
                    }}
                  >
                    {documentation || fallbackContent}
                  </ReactMarkdown>
                </div>
              )}
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
};

export default DatabaseDetailsDialog;
