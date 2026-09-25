import axios from 'axios';
import { useCallback, useEffect, useRef, useState } from 'react';
export { ChannelProvider, useChannels } from './channels';

// Same-origin API/media paths also work behind the production reverse proxy.
export const API = '/api';
export const MEDIA = '';
export const api = axios.create({ baseURL: API, withCredentials: true });

// When the backend requires login, an expired or missing session sends the user to /login.
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (
      axios.isAxiosError(error) &&
      error.response?.status === 401 &&
      !String(error.config?.url || '').startsWith('/auth') &&
      window.location.pathname !== '/login'
    ) {
      window.location.assign('/login');
    }
    return Promise.reject(error);
  },
);

async function request<T>(path: string, method: string, body?: unknown): Promise<T> {
  if (!path.startsWith('/api/')) throw new Error('Expected a relative /api path');
  try {
    const result = await api.request<T>({ url: path.slice(4), method, data: body });
    return result.data;
  } catch (error) {
    if (axios.isAxiosError(error)) {
      const detail: unknown = error.response?.data?.detail;
      throw new Error(typeof detail === 'string' ? detail : 'Request failed. Check the input and retry.');
    }
    throw error;
  }
}
export const apiGet = <T,>(path: string) => request<T>(path, 'GET');
export const apiPost = <T,>(path: string, body?: unknown) => request<T>(path, 'POST', body);
export const apiPut = <T,>(path: string, body: unknown) => request<T>(path, 'PUT', body);
export const apiPatch = <T,>(path: string, body: unknown) => request<T>(path, 'PATCH', body);
export const apiDelete = <T,>(path: string) => request<T>(path, 'DELETE');

// Compatibility for the imported CRA pages. New features use TanStack Query.
export function usePoll<T = any>(url: string, ms = 4000): [T | null, () => Promise<void>, unknown] {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const urlRef = useRef(url);
  urlRef.current = url;
  const load = useCallback(async () => {
    try { setData((await api.get<T>(urlRef.current)).data); setError(null); }
    catch (e) { setError(e); }
  }, []);
  useEffect(() => { void load(); const timer = setInterval(load, ms); return () => clearInterval(timer); }, [load, ms, url]);
  return [data, load, error];
}
export const channelQ = (selected: string) => selected && selected !== 'all' ? `?channel_id=${encodeURIComponent(selected)}` : '';