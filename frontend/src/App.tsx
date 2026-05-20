import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Toaster as Sonner } from "@/components/ui/sonner";
import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
import { AuthProvider } from "@/contexts/AuthContext";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import Landing from "./pages/Landing";
import Auth from "./pages/Auth";
import Dashboard from "./pages/app/Dashboard";
import Bots from "./pages/app/Bots";
import Signals from "./pages/app/Signals";
import Trades from "./pages/app/Trades";
import Bridge from "./pages/app/Bridge";
import Billing from "./pages/app/Billing";
import Transactions from "./pages/app/Transactions";
import Referrals from "./pages/app/Referrals";
import Settings from "./pages/app/Settings";
import Admin from "./pages/app/Admin";
import NotFound from "./pages/NotFound";

const queryClient = new QueryClient();

const App = () => (
  <QueryClientProvider client={queryClient}>
    <TooltipProvider>
      <Toaster />
      <Sonner theme="dark" />
      <AuthProvider>
        <BrowserRouter>
          <Routes>
            <Route path="/" element={<Landing />} />
            <Route path="/auth" element={<Auth />} />
            <Route path="/app" element={<ProtectedRoute><Dashboard /></ProtectedRoute>} />
            <Route path="/app/bots" element={<ProtectedRoute><Bots /></ProtectedRoute>} />
            <Route path="/app/signals" element={<ProtectedRoute><Signals /></ProtectedRoute>} />
            <Route path="/app/trades" element={<ProtectedRoute><Trades /></ProtectedRoute>} />
            <Route path="/app/bridge" element={<ProtectedRoute><Bridge /></ProtectedRoute>} />
            <Route path="/app/billing" element={<ProtectedRoute><Billing /></ProtectedRoute>} />
            <Route path="/app/transactions" element={<ProtectedRoute><Transactions /></ProtectedRoute>} />
            <Route path="/app/referrals" element={<ProtectedRoute><Referrals /></ProtectedRoute>} />
            <Route path="/app/settings" element={<ProtectedRoute><Settings /></ProtectedRoute>} />
            <Route path="/app/admin" element={<ProtectedRoute><Admin /></ProtectedRoute>} />
            <Route path="*" element={<NotFound />} />
          </Routes>
        </BrowserRouter>
      </AuthProvider>
    </TooltipProvider>
  </QueryClientProvider>
);

export default App;
