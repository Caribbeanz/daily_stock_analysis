import axios from 'axios';
import { API_BASE_URL } from '../utils/constants';
import { attachParsedApiError } from './error';

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30000,
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  },
});

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      const path = window.location.pathname + window.location.search;
      // 大屏（P4 · FR-14.1.4）为**免登录常驻**页面：不因 401 跳转登录页，
      // 由页面自行降级展示（快照端点已在后端鉴权白名单放行）。
      const isStandaloneScreen = window.location.pathname.startsWith('/screen');
      if (!path.startsWith('/login') && !isStandaloneScreen) {
        const redirect = encodeURIComponent(path);
        window.location.assign(`/login?redirect=${redirect}`);
      }
    }
    attachParsedApiError(error);
    return Promise.reject(error);
  }
);

export default apiClient;
