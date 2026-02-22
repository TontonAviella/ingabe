import { Eye, EyeOff, GripVertical, Loader2, MoreHorizontal, Paintbrush } from 'lucide-react';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { Input } from '@/components/ui/input';
import { Slider } from '@/components/ui/slider';

interface DropdownAction {
  label: string;
  action: (layerId: string) => void;
  disabled?: boolean;
}

interface LayerListItemProps {
  name: string;
  nameClassName?: string;
  status?: 'added' | 'removed' | 'edited' | 'existing';
  isActive?: boolean;
  progressBar?: number | null;
  hoverText?: string;
  normalText?: string;
  legendSymbol?: React.ReactNode;
  onClick?: (e: React.MouseEvent<HTMLButtonElement>) => void;
  className?: string;
  displayAsDiff?: boolean;
  layerId: string;
  dropdownActions?: {
    [key: string]: DropdownAction;
  };
  isVisible?: boolean;
  onToggleVisibility?: (layerId: string) => void;
  onRename?: (layerId: string, newName: string) => void;
  title?: string;
  isLoading?: boolean;
  /** Current opacity 0–1. When provided, a slider appears on hover. */
  opacity?: number;
  onOpacityChange?: (layerId: string, opacity: number) => void;
  /** Current override color (hex). When provided with onColorChange, shows a "Change color" dropdown item. */
  currentColor?: string;
  onColorChange?: (layerId: string, color: string) => void;
}

export const LayerListItem: React.FC<LayerListItemProps> = ({
  name,
  nameClassName = '',
  status = 'existing',
  isActive = false,
  progressBar = null,
  hoverText,
  normalText,
  legendSymbol,
  onClick,
  className = '',
  displayAsDiff = false,
  layerId,
  dropdownActions = {},
  isVisible = true,
  onToggleVisibility,
  onRename,
  title,
  isLoading = false,
  opacity,
  onOpacityChange,
  currentColor,
  onColorChange,
}) => {
  const colorInputRef = useRef<HTMLInputElement>(null);
  const [nameValue, setNameValue] = useState(name);
  const [isDebouncing, setIsDebouncing] = useState(false);
  const debounceTimeoutRef = useRef<NodeJS.Timeout | null>(null);

  const debouncedSave = useCallback(
    (value: string) => {
      const trimmedValue = value.trim();

      if (trimmedValue && trimmedValue !== name && onRename) {
        onRename(layerId, trimmedValue);
      }
      setIsDebouncing(false);
    },
    [name, onRename, layerId],
  );

  const handleNameChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const newValue = e.target.value;
    setNameValue(newValue);

    if (debounceTimeoutRef.current) {
      clearTimeout(debounceTimeoutRef.current);
    }

    setIsDebouncing(true);

    debounceTimeoutRef.current = setTimeout(() => {
      debouncedSave(newValue);
    }, 1000);
  };

  // Update local name when prop changes
  useEffect(() => {
    setNameValue(name);
  }, [name]);

  // Cleanup timeout on unmount
  useEffect(() => {
    return () => {
      if (debounceTimeoutRef.current) {
        clearTimeout(debounceTimeoutRef.current);
      }
    };
  }, []);
  let liClassName = '';

  if (displayAsDiff) {
    if (status === 'added') {
      liClassName += ' bg-green-900 hover:bg-green-800';
    } else if (status === 'removed') {
      liClassName += ' bg-red-100 dark:bg-red-900 hover:bg-red-200 dark:hover:bg-red-800';
    } else if (status === 'edited') {
      liClassName += ' bg-yellow-100 dark:bg-yellow-800 hover:bg-yellow-200 dark:hover:bg-yellow-700';
    } else {
      liClassName += ' hover:bg-slate-100 dark:hover:bg-gray-600 dark:focus:bg-gray-600';
    }
  } else {
    liClassName += ' hover:bg-slate-100 dark:hover:bg-gray-600 dark:focus:bg-gray-600';
  }

  if (isActive) {
    liClassName += ' animate-pulse';
  }

  return (
    <div className={`${liClassName} flex items-center px-2 py-1 gap-2 group w-full ${className}`} title={title}>
      {/* Hidden native color picker — triggered programmatically by the dropdown item */}
      {onColorChange && (
        <input
          ref={colorInputRef}
          type="color"
          value={currentColor ?? '#888888'}
          className="sr-only"
          onChange={(e) => onColorChange(layerId, e.target.value)}
          onClick={(e) => e.stopPropagation()}
        />
      )}
      <div className="w-4 h-4 flex-shrink-0 flex items-center justify-center">
        {isLoading ? (
          <Loader2 className="w-3 h-3 animate-spin text-gray-300" />
        ) : (
          <GripVertical className="w-3 h-3 text-gray-400 opacity-0 group-hover:opacity-100 transition-opacity cursor-grab" />
        )}
      </div>

      <div className="flex items-center gap-2 flex-1">
        {onRename ? (
          <>
            <Input
              value={nameValue}
              onChange={handleNameChange}
              className={`border-0 rounded-none !bg-transparent p-0 h-auto !text-sm font-medium focus-visible:ring-0 focus-visible:ring-offset-0 shadow-none outline-none flex-1 ${nameClassName}`}
              title={name}
            />
            {isDebouncing && <Loader2 className="h-3 w-3 animate-spin text-gray-400" />}
          </>
        ) : (
          <span className={`font-medium truncate ${nameClassName}`} title={name}>
            {nameValue.length > 26 ? nameValue.slice(0, 26) + '...' : nameValue}
          </span>
        )}
      </div>
      <div className="flex items-center gap-2">
        {progressBar !== null && (
          <div className="w-12 h-1 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
            <div
              className="h-full bg-blue-500 transition-all duration-300 ease-out"
              style={{ width: `${Math.max(0, Math.min(100, progressBar * 100))}%` }}
            />
          </div>
        )}
        {/* Opacity slider — visible on hover when onOpacityChange is provided */}
        {onOpacityChange ? (
          <>
            {/* Normal text hidden on hover */}
            <span className="text-xs text-slate-500 dark:text-gray-400 group-hover:hidden">{normalText}</span>
            {/* Slider + percentage shown on hover */}
            <div className="hidden group-hover:flex items-center gap-1 w-24 flex-shrink-0">
              <Slider
                min={0}
                max={1}
                step={0.05}
                value={[opacity ?? 1]}
                onValueChange={([val]) => onOpacityChange(layerId, val)}
                className="w-14"
                onClick={(e) => e.stopPropagation()}
              />
              <span className="text-xs text-slate-400 w-7 text-right tabular-nums">{Math.round((opacity ?? 1) * 100)}%</span>
            </div>
          </>
        ) : (
          (hoverText || normalText) && (
            <span className="text-xs text-slate-500 dark:text-gray-400">
              {hoverText && normalText ? (
                <>
                  <span className="group-hover:hidden">{normalText}</span>
                  <span className="hidden group-hover:inline">{hoverText}</span>
                </>
              ) : (
                hoverText || normalText
              )}
            </span>
          )
        )}
        <div className="flex items-center gap-1">
          <div className="w-5 h-5 flex-shrink-0 relative">
            <div className="absolute inset-0 flex items-center justify-center group-hover:hidden">{legendSymbol}</div>
            <button
              className="absolute inset-0 flex items-center justify-center rounded cursor-pointer hover:bg-slate-200 dark:hover:bg-gray-500 opacity-0 group-hover:opacity-100 transition-opacity"
              onClick={(e) => {
                e.stopPropagation();
                onToggleVisibility?.(layerId);
              }}
              aria-label={isVisible ? 'Hide layer' : 'Show layer'}
            >
              {isVisible ? <Eye className="w-4 h-4" /> : <EyeOff className="w-4 h-4" />}
            </button>
          </div>

          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                className="w-5 h-5 flex items-center justify-center rounded cursor-pointer opacity-0 group-hover:opacity-100 transition-all hover:bg-slate-200 dark:hover:bg-gray-500"
                onClick={(e) => {
                  e.stopPropagation();
                  onClick?.(e);
                }}
              >
                <MoreHorizontal className="w-4 h-4 text-gray-400 hover:text-white transition-colors" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent>
              {Object.entries(dropdownActions).map(([key, actionConfig]) => (
                <DropdownMenuItem
                  key={key}
                  disabled={actionConfig.disabled}
                  onClick={() => actionConfig.action(layerId)}
                  className="border-transparent hover:border-gray-600 hover:cursor-pointer border"
                >
                  {actionConfig.label}
                </DropdownMenuItem>
              ))}
              {onColorChange && (
                <DropdownMenuItem
                  className="border-transparent hover:border-gray-600 hover:cursor-pointer border flex items-center gap-2"
                  onClick={(e) => {
                    e.stopPropagation();
                    colorInputRef.current?.click();
                  }}
                >
                  <Paintbrush className="w-3.5 h-3.5" />
                  <span>Change color</span>
                  {currentColor && (
                    <span
                      className="ml-auto w-4 h-4 rounded-sm border border-gray-400 flex-shrink-0"
                      style={{ backgroundColor: currentColor }}
                    />
                  )}
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
    </div>
  );
};
