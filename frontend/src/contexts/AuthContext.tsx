import {
  createContext,
  useContext,
  useEffect,
  useState,
  ReactNode,
  useCallback,
} from "react";
import { api, apiPost, errMessage } from "@/api/client";

export type AppUser = {
  id: string;
  email: string;
  display_name?: string | null;
  role: "admin" | "user";
  avatar_url?: string | null;
  referral_code?: string | null;
  referred_by?: string | null;
  disabled?: boolean;
  created_at?: string;
};

interface AuthCtx {
  user: AppUser | null;
  loading: boolean;
  signIn: (email: string, password: string) => Promise<void>;
  signUp: (
    email: string,
    password: string,
    displayName?: string,
    referralCode?: string,
  ) => Promise<void>;
  signOut: () => Promise<void>;
  refresh: () => Promise<void>;
}

const Ctx = createContext<AuthCtx>({
  user: null,
  loading: true,
  signIn: async () => {},
  signUp: async () => {},
  signOut: async () => {},
  refresh: async () => {},
});

export const AuthProvider = ({ children }: { children: ReactNode }) => {
  const [user, setUser] = useState<AppUser | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const { data } = await api.get<AppUser>("/auth/me");
      setUser(data);
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const signIn = async (email: string, password: string) => {
    try {
      const u = await apiPost<AppUser>("/auth/login", { email, password });
      setUser(u);
    } catch (e) {
      throw new Error(errMessage(e));
    }
  };

  const signUp = async (
    email: string,
    password: string,
    displayName?: string,
    referralCode?: string,
  ) => {
    try {
      const u = await apiPost<AppUser>("/auth/register", {
        email,
        password,
        display_name: displayName,
        referral_code: referralCode || undefined,
      });
      setUser(u);
    } catch (e) {
      throw new Error(errMessage(e));
    }
  };

  const signOut = async () => {
    try {
      await apiPost("/auth/logout");
    } catch {
      // ignore
    }
    setUser(null);
  };

  return (
    <Ctx.Provider value={{ user, loading, signIn, signUp, signOut, refresh }}>
      {children}
    </Ctx.Provider>
  );
};

export const useAuth = () => useContext(Ctx);
