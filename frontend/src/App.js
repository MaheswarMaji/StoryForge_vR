import "@/App.css";
import { Toaster } from "@/components/ui/sonner";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { ChannelProvider } from "@/lib/api";
import Layout from "@/components/Layout";
import ProtectedRoute from "@/components/ProtectedRoute";
import LoginPage from "@/pages/LoginPage";
import DashboardPage from "@/pages/DashboardPage";
import UploadPage from "@/pages/UploadPage";
import LibraryPage from "@/pages/LibraryPage";
import StoryDetailPage from "@/pages/StoryDetailPage";
import ChannelsPage from "@/pages/ChannelsPage";
import SettingsPage from "@/pages/SettingsPage";
import SocialPage from "@/pages/SocialPage";
import NewsDeskPage from "@/pages/NewsDeskPage";
import CreatePage from "@/pages/CreatePage";
import AdminPage from "@/pages/AdminPage";
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } } });

function AppRouter() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<ProtectedRoute><Layout /></ProtectedRoute>}>
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={<DashboardPage />} />
        <Route path="/upload" element={<UploadPage />} />
        <Route path="/create" element={<CreatePage />} />
        <Route path="/stories" element={<LibraryPage />} />
        <Route path="/stories/:id" element={<StoryDetailPage />} />
        <Route path="/news" element={<NewsDeskPage />} />
        <Route path="/social" element={<SocialPage />} />
        <Route path="/channels" element={<ChannelsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/admin" element={<AdminPage />} />
      </Route>
    </Routes>
  );
}

function App() {
  return (
    <QueryClientProvider client={queryClient}><ChannelProvider>
      <BrowserRouter>
        <AppRouter />
      </BrowserRouter>
      <Toaster position="bottom-right" richColors closeButton theme="dark" />
    </ChannelProvider></QueryClientProvider>
  );
}

export default App;
