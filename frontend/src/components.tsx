import { useState } from 'react';

export function useToast(): [string | null, (msg: string) => void] {
  const [toast, setToast] = useState<string | null>(null);
  const show = (msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(null), 1800);
  };
  return [toast, show];
}

export function Toast({ msg }: { msg: string | null }) {
  if (!msg) return null;
  return <div className="toast">{msg}</div>;
}