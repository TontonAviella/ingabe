import { fetchMaybeAuth } from '@mundi/ee';
import { AlertTriangle, FileText, Loader2, Upload } from 'lucide-react';
import React, { useRef, useState } from 'react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';

const ALLOWED_EXTENSIONS = ['.pdf', '.docx', '.xlsx', '.pptx', '.txt', '.md', '.csv'];
const MAX_FILE_SIZE = 50 * 1024 * 1024; // 50 MB
const ACCEPT_STRING = ALLOWED_EXTENSIONS.join(',');

function validateFile(f: File): string | null {
  const ext = `.${f.name.split('.').pop()?.toLowerCase()}`;
  if (!ALLOWED_EXTENSIONS.includes(ext)) {
    return `Unsupported file type "${ext}". Allowed: ${ALLOWED_EXTENSIONS.join(', ')}`;
  }
  if (f.size > MAX_FILE_SIZE) {
    return `File size ${(f.size / 1024 / 1024).toFixed(1)} MB exceeds limit of 50 MB.`;
  }
  if (f.size === 0) {
    return 'File is empty.';
  }
  return null;
}

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

interface UploadDocumentProps {
  isOpen: boolean;
  onClose: () => void;
  projectId?: string;
}

const UploadDocument: React.FC<UploadDocumentProps> = ({ isOpen, onClose, projectId }) => {
  const [file, setFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleClose = () => {
    setFile(null);
    setError('');
    setLoading(false);
    onClose();
  };

  const handleFileSelect = (f: File) => {
    setError('');
    const validationError = validateFile(f);
    if (validationError) {
      setError(validationError);
      setFile(null);
      return;
    }
    setFile(f);
  };

  const handleUpload = async () => {
    if (!file || !projectId) return;
    setLoading(true);
    setError('');

    try {
      const formData = new FormData();
      formData.append('file', file);

      const response = await fetchMaybeAuth(`/api/projects/${projectId}/upload-document`, {
        method: 'POST',
        body: formData,
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => null);
        throw new Error(errorData?.detail || `Upload failed (HTTP ${response.status})`);
      }

      const result = await response.json();
      toast.success(`"${result.title}" uploaded to Brain. Sage can now search it.`);
      handleClose();
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Upload failed';
      setError(message);
      toast.error(message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && handleClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Upload Document to Brain</DialogTitle>
          <DialogDescription>
            Upload a document so Sage can search its contents. Supports PDF, Word, Excel, PowerPoint, and text files.
          </DialogDescription>
        </DialogHeader>

        <div
          className="border-2 border-dashed rounded-lg p-6 text-center cursor-pointer transition-colors hover:border-blue-400 hover:bg-blue-50/5"
          onClick={() => fileInputRef.current?.click()}
          onKeyDown={(e) => e.key === 'Enter' && fileInputRef.current?.click()}
          onDrop={(e) => {
            e.preventDefault();
            const dropped = e.dataTransfer.files[0];
            if (dropped) handleFileSelect(dropped);
          }}
          onDragOver={(e) => e.preventDefault()}
        >
          <input
            ref={fileInputRef}
            type="file"
            accept={ACCEPT_STRING}
            onChange={(e) => {
              const selected = e.target.files?.[0];
              if (selected) handleFileSelect(selected);
            }}
            className="hidden"
          />
          {file ? (
            <div className="flex items-center justify-center gap-2">
              <FileText className="h-5 w-5 text-blue-500" />
              <div className="text-left">
                <p className="text-sm font-medium truncate max-w-[280px]">{file.name}</p>
                <p className="text-xs text-muted-foreground">{formatSize(file.size)}</p>
              </div>
            </div>
          ) : (
            <div className="space-y-2">
              <Upload className="h-8 w-8 mx-auto text-muted-foreground" />
              <p className="text-sm text-muted-foreground">Click or drag a file here</p>
              <p className="text-xs text-muted-foreground">PDF, DOCX, XLSX, PPTX, TXT, MD, CSV (max 50 MB)</p>
            </div>
          )}
        </div>

        {error && (
          <div className="flex items-start gap-2 text-red-500 text-sm">
            <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        <DialogFooter>
          <Button variant="ghost" onClick={handleClose} disabled={loading}>
            Cancel
          </Button>
          <Button onClick={handleUpload} disabled={!file || loading || !projectId}>
            {loading ? (
              <>
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                Uploading...
              </>
            ) : (
              'Upload to Brain'
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};

export default UploadDocument;
