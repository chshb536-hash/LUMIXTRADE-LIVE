// Lightweight API client for the Aurum FX FastAPI backend.
// Replaces Supabase direct DB access. Uses httpOnly cookies for auth (primary)
// with Bearer fallback from localStorage (e.g., long-lived scripts).

import axios, { AxiosError, AxiosInstance, AxiosRequestConfig } from "axios";

const BACKEND_URL =
  process.env.REACT_APP_BACKEND_URL || "http://localhost:8001";

export const api: AxiosInstance = axios.create({
  baseURL: `${BACKEND_URL}/api`,
  withCredentials: true,
  timeout: 30000,
});

// Optional bearer fallback (unused in normal browser flow but useful for scripts)
const BEARER_KEY = "aurum_access_token";
export const setBearer = (token: string | null) => {
  if (token) localStorage.setItem(BEARER_KEY, token);
  else localStorage.removeItem(BEARER_KEY);
};
api.interceptors.request.use((config) => {
  const t = localStorage.getItem(BEARER_KEY);
  if (t && config.headers) {
    config.headers["Authorization"] = `Bearer ${t}`;
  }
  return config;
});

export function formatApiErrorDetail(detail: any): string {
  if (detail == null) return "Something went wrong. Please try again.";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail))
    return detail
      .map((e) =>
        e && typeof e.msg === "string" ? e.msg : JSON.stringify(e),
      )
      .filter(Boolean)
      .join(" ");
  if (detail && typeof detail.msg === "string") return detail.msg;
  return String(detail);
}

export function errMessage(e: unknown): string {
  if (axios.isAxiosError(e)) {
    const ax = e as AxiosError<any>;
    return (
      formatApiErrorDetail(ax.response?.data?.detail) ||
      ax.message ||
      "Network error"
    );
  }
  return (e as Error)?.message ?? "Unknown error";
}

// Convenience wrappers
export const apiGet = async <T = any>(
  url: string,
  config?: AxiosRequestConfig,
) => (await api.get<T>(url, config)).data;

export const apiPost = async <T = any>(
  url: string,
  body?: any,
  config?: AxiosRequestConfig,
) => (await api.post<T>(url, body, config)).data;

export const apiPatch = async <T = any>(
  url: string,
  body?: any,
  config?: AxiosRequestConfig,
) => (await api.patch<T>(url, body, config)).data;

export const apiDelete = async <T = any>(
  url: string,
  config?: AxiosRequestConfig,
) => (await api.delete<T>(url, config)).data;
