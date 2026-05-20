import { useEffect, useState } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { z } from "zod";
import { useAuth } from "@/contexts/AuthContext";
import { Panel } from "@/components/Panel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { toast } from "sonner";
import { ArrowLeft, Gift } from "lucide-react";

const emailSchema = z.string().trim().email("Invalid email").max(255);
const passwordSchema = z.string().min(8, "Min 8 characters").max(72);

export default function Auth() {
  const { user, loading, signIn, signUp } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const refFromUrl = params.get("ref") || "";
  const [activeTab, setActiveTab] = useState<"in" | "up">(refFromUrl ? "up" : "in");
  const [busy, setBusy] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [referralCode, setReferralCode] = useState(refFromUrl.toUpperCase());

  useEffect(() => {
    if (refFromUrl) setReferralCode(refFromUrl.toUpperCase());
  }, [refFromUrl]);

  if (!loading && user) return <Navigate to="/app" replace />;

  const handle = async (mode: "in" | "up") => {
    try {
      emailSchema.parse(email);
      passwordSchema.parse(password);
    } catch (e) {
      if (e instanceof z.ZodError) {
        toast.error(e.issues[0].message);
        return;
      }
    }
    setBusy(true);
    try {
      if (mode === "up") {
        await signUp(email, password, displayName || email.split("@")[0], referralCode.trim() || undefined);
        toast.success("Account created.");
        navigate("/app");
      } else {
        await signIn(email, password);
        toast.success("Signed in.");
        navigate("/app");
      }
    } catch (e: any) {
      toast.error(e.message ?? "Authentication failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen flex flex-col">
      <div className="px-6 py-4">
        <Link to="/" className="font-mono text-xs text-muted-foreground hover:text-primary tracking-widest inline-flex items-center gap-1.5" data-testid="back-to-landing-link">
          <ArrowLeft className="w-3 h-3" /> LUMIXTRADE
        </Link>
      </div>
      <div className="flex-1 flex items-center justify-center px-4">
        <div className="w-full max-w-md animate-fade-up">
          <div className="text-center mb-6">
            <div className="font-mono text-[10px] tracking-[0.3em] text-primary mb-2">SECURE TERMINAL ACCESS</div>
            <h1 className="font-display text-2xl font-bold">Sign in to your terminal</h1>
            <p className="text-sm text-muted-foreground mt-1">Auto-trading for forex & gold on MT5.</p>
            {refFromUrl && (
              <div className="mt-3 mx-auto inline-flex items-center gap-2 font-mono text-[10px] tracking-widest bg-primary/10 border border-primary/30 text-primary rounded px-3 py-1.5" data-testid="auth-ref-badge">
                <Gift className="w-3 h-3" /> REFERRED BY {refFromUrl.toUpperCase()}
              </div>
            )}
          </div>
          <Panel>
            <Tabs value={activeTab} onValueChange={(v) => setActiveTab(v as any)} className="w-full">
              <TabsList className="grid grid-cols-2 w-full font-mono text-[11px] tracking-widest">
                <TabsTrigger value="in" data-testid="auth-tab-sign-in">SIGN IN</TabsTrigger>
                <TabsTrigger value="up" data-testid="auth-tab-sign-up">SIGN UP</TabsTrigger>
              </TabsList>
              <TabsContent value="in" className="space-y-3 pt-4">
                <Field label="EMAIL" id="e1" type="email" value={email} onChange={setEmail} testid="signin-email-input" />
                <Field label="PASSWORD" id="p1" type="password" value={password} onChange={setPassword} testid="signin-password-input" />
                <Button onClick={() => handle("in")} disabled={busy} className="w-full font-mono tracking-widest" data-testid="signin-submit-btn">
                  {busy ? "AUTHENTICATING…" : "SIGN IN →"}
                </Button>
              </TabsContent>
              <TabsContent value="up" className="space-y-3 pt-4">
                <Field label="DISPLAY NAME" id="d2" value={displayName} onChange={setDisplayName} testid="signup-displayname-input" />
                <Field label="EMAIL" id="e2" type="email" value={email} onChange={setEmail} testid="signup-email-input" />
                <Field label="PASSWORD" id="p2" type="password" value={password} onChange={setPassword} testid="signup-password-input" />
                <Field label="REFERRAL CODE (OPTIONAL)" id="r2" value={referralCode} onChange={(v) => setReferralCode(v.toUpperCase())} testid="signup-referral-input" />
                <Button onClick={() => handle("up")} disabled={busy} className="w-full font-mono tracking-widest" data-testid="signup-submit-btn">
                  {busy ? "CREATING…" : "CREATE ACCOUNT →"}
                </Button>
                <p className="text-[10px] text-muted-foreground font-mono tracking-wider">By signing up you accept the risk disclosure. Forex & CFD trading carries substantial risk.</p>
              </TabsContent>
            </Tabs>
          </Panel>
        </div>
      </div>
    </div>
  );
}

const Field = ({ label, id, value, onChange, type = "text", testid }: { label: string; id: string; value: string; onChange: (v: string) => void; type?: string; testid?: string }) => (
  <div className="space-y-1.5">
    <Label htmlFor={id} className="font-mono text-[10px] tracking-widest text-muted-foreground">{label}</Label>
    <Input id={id} type={type} value={value} onChange={(e) => onChange(e.target.value)}
      data-testid={testid}
      className="font-mono bg-input border-border focus-visible:ring-primary" />
  </div>
);
