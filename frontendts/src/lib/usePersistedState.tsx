import { useEffect, useRef, useState } from 'react';

type Json = string | number | boolean | null | Json[] | { [k: string]: Json };

export function usePersistedState<T extends Json>(
  baseKey: string,
  deps: (string | number | boolean)[],
  initial: T,
  storage: Storage = window.sessionStorage,
): [T, React.Dispatch<React.SetStateAction<T>>] {
  const storageKey = `${baseKey}:${deps.join('|')}`;

  const [value, setValue] = useState<T>(() => {
    const raw = storage.getItem(storageKey);
    return raw ? (JSON.parse(raw) as T) : initial;
  });

  const activeKeyRef = useRef(storageKey);

  useEffect(() => {
    if (activeKeyRef.current !== storageKey) {
      const raw = storage.getItem(storageKey);
      setValue(raw ? (JSON.parse(raw) as T) : initial);
      activeKeyRef.current = storageKey;
      return;
    }

    storage.setItem(storageKey, JSON.stringify(value));
  }, [storageKey, value, initial, storage]);

  return [value, setValue];
}
