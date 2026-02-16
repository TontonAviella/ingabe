import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Loader2 } from 'lucide-react';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { Input } from '@/components/ui/input';

interface EditableTitleProps {
  projectId: string;
  title?: string;
  placeholder?: string;
  className?: string;
}

const EditableTitle: React.FC<EditableTitleProps> = ({ projectId, title = '', placeholder = 'Enter title here', className = '' }) => {
  const queryClient = useQueryClient();
  const [titleValue, setTitleValue] = useState(title);
  const [isDebouncing, setIsDebouncing] = useState(false);
  const debounceTimeoutRef = useRef<NodeJS.Timeout | null>(null);

  const updateTitleMutation = useMutation({
    mutationFn: async (newTitle: string) => {
      const response = await fetch(`/api/projects/${projectId}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ title: newTitle }),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({ detail: response.statusText }));
        throw new Error(errorData.detail || response.statusText);
      }

      return response.json();
    },
    onSuccess: () => {
      // refresh the data
      queryClient.invalidateQueries({ queryKey: ['project', projectId] });
      queryClient.invalidateQueries({ queryKey: ['project', projectId, 'map'] });
      setIsDebouncing(false);
    },
    onError: (error: Error) => {
      toast.error(`Failed to update title: ${error.message}`);
      setTitleValue(title);
      setIsDebouncing(false);
    },
  });

  const debouncedSave = useCallback(
    (value: string) => {
      const trimmedValue = value.trim();
      const currentTitle = title;

      if (trimmedValue !== currentTitle) {
        updateTitleMutation.mutate(trimmedValue);
      } else {
        setIsDebouncing(false);
      }
    },
    [title, updateTitleMutation],
  );

  const handleTitleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const newValue = e.target.value;
    setTitleValue(newValue);

    if (debounceTimeoutRef.current) {
      clearTimeout(debounceTimeoutRef.current);
    }

    setIsDebouncing(true);

    debounceTimeoutRef.current = setTimeout(() => {
      debouncedSave(newValue);
    }, 1000);
  };

  // Update local title when prop changes
  useEffect(() => {
    setTitleValue(title);
  }, [title]);

  // Cleanup timeout on unmount
  useEffect(() => {
    return () => {
      if (debounceTimeoutRef.current) {
        clearTimeout(debounceTimeoutRef.current);
      }
    };
  }, []);

  return (
    <div className="flex items-center gap-2 flex-1">
      <Input
        value={titleValue}
        onChange={handleTitleChange}
        className={`border-0 rounded-none !bg-transparent p-0 h-auto !text-sm font-semibold focus-visible:ring-0 focus-visible:ring-offset-0 shadow-none outline-none flex-1 ${className}`}
        disabled={updateTitleMutation.isPending}
        placeholder={placeholder}
      />
      {(isDebouncing || updateTitleMutation.isPending) && <Loader2 className="h-3 w-3 animate-spin text-gray-400" />}
    </div>
  );
};

export default EditableTitle;
