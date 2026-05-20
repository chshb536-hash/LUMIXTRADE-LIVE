import { useAuth } from "@/contexts/AuthContext";

export const useIsAdmin = (): boolean | null => {
  const { user, loading } = useAuth();
  if (loading) return null;
  if (!user) return false;
  return user.role === "admin";
};
