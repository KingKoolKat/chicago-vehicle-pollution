(function () {
    const DEPLOYED_AUTH_API_URL = "https://kingkoolkat--ecotrack-inference-auth.modal.run";
    const SESSION_TOKEN_KEY = "ecotrack_session_token_v1";
    const SESSION_USER_KEY = "ecotrack_session_user_v1";
    const SESSION_VALIDATED_AT_KEY = "ecotrack_session_validated_at_v1";
    let resolvedAuthApiUrl = "";

    function uniq(values) {
        const seen = new Set();
        const out = [];
        for (const value of values) {
            const clean = String(value || "").trim();
            if (!clean || seen.has(clean)) continue;
            seen.add(clean);
            out.push(clean);
        }
        return out;
    }

    function getAuthApiCandidates() {
        const cached = resolvedAuthApiUrl || sessionStorage.getItem("ecotrack_auth_api_url") || "";
        return uniq([
            window.AUTH_API_URL,
            cached,
            DEPLOYED_AUTH_API_URL,
            "/auth"
        ]);
    }

    function rememberAuthApiUrl(url) {
        resolvedAuthApiUrl = String(url || "").trim();
        if (!resolvedAuthApiUrl) return;
        try {
            sessionStorage.setItem("ecotrack_auth_api_url", resolvedAuthApiUrl);
        } catch (error) {
            console.error("Unable to cache auth API URL", error);
        }
    }

    function normalizeEmail(email) {
        return (email || "").trim().toLowerCase();
    }

    function sanitizeUser(user) {
        if (!user) return null;
        return {
            id: user.id,
            name: user.name || "",
            email: normalizeEmail(user.email),
            provider: user.provider || "local",
            role: user.role || "resident",
            avatarUrl: user.avatarUrl || ""
        };
    }

    function getStoredToken() {
        return sessionStorage.getItem(SESSION_TOKEN_KEY) || "";
    }

    function getSessionToken() {
        return getStoredToken();
    }

    function setStoredSession(token, user) {
        if (!token || !user) {
            clearStoredSession();
            return;
        }
        sessionStorage.setItem(SESSION_TOKEN_KEY, token);
        sessionStorage.setItem(SESSION_USER_KEY, JSON.stringify(sanitizeUser(user)));
        sessionStorage.setItem(SESSION_VALIDATED_AT_KEY, String(Date.now()));
    }

    function clearStoredSession() {
        sessionStorage.removeItem(SESSION_TOKEN_KEY);
        sessionStorage.removeItem(SESSION_USER_KEY);
        sessionStorage.removeItem(SESSION_VALIDATED_AT_KEY);
    }

    function getSessionUser() {
        try {
            const raw = sessionStorage.getItem(SESSION_USER_KEY);
            if (!raw) return null;
            return sanitizeUser(JSON.parse(raw));
        } catch (error) {
            console.error("Unable to parse session user", error);
            return null;
        }
    }

    function getLastValidatedAt() {
        const raw = sessionStorage.getItem(SESSION_VALIDATED_AT_KEY);
        const value = Number(raw || 0);
        return Number.isFinite(value) ? value : 0;
    }

    async function refreshSession(options = {}) {
        const force = options.force === true;
        const maxAgeMs = Number(options.maxAgeMs || 0);
        const token = getStoredToken();
        if (!token) return null;

        const currentUser = getSessionUser();
        if (!force && currentUser && maxAgeMs > 0) {
            const age = Date.now() - getLastValidatedAt();
            if (age >= 0 && age < maxAgeMs) {
                return currentUser;
            }
        }

        const result = await authRequest("session", { token });
        if (result.ok && result.user) {
            setStoredSession(token, result.user);
            return getSessionUser();
        }

        const message = String(result.message || "");
        const invalidSession = /invalid session|need to be logged in/i.test(message);
        if (invalidSession) {
            clearStoredSession();
            return null;
        }

        // Keep cached user if backend is temporarily unreachable.
        return currentUser;
    }

    async function authRequest(action, payload = {}) {
        const attempted = [];
        const nonOkResponses = [];
        const body = JSON.stringify({ action, ...payload });
        const candidates = getAuthApiCandidates();

        for (const url of candidates) {
            attempted.push(url);
            try {
                const response = await fetch(url, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body
                });
                let data = {};
                try {
                    data = await response.json();
                } catch (error) {
                    data = {};
                }
                if (!response.ok) {
                    nonOkResponses.push(`${url} -> ${response.status}`);
                    continue;
                }
                rememberAuthApiUrl(url);
                return data;
            } catch (error) {
                console.error(`Auth request failed for action '${action}' via ${url}`, error);
                continue;
            }
        }

        return {
            ok: false,
            message: nonOkResponses.length
                ? `Auth server rejected requests. Responses: ${nonOkResponses.join(" | ")}. Tried: ${attempted.join(", ")}`
                : `Unable to reach authentication server. Set window.AUTH_API_URL if needed. Tried: ${attempted.join(", ")}`
        };
    }

    async function signUp({ name, email, password }) {
        const result = await authRequest("signup", {
            name: (name || "").trim(),
            email: normalizeEmail(email),
            password: password || ""
        });
        if (result.ok) {
            setStoredSession(result.token, result.user);
        }
        return result;
    }

    async function login({ email, password }) {
        const result = await authRequest("login", {
            email: normalizeEmail(email),
            password: password || ""
        });
        if (result.ok) {
            setStoredSession(result.token, result.user);
        }
        return result;
    }

    async function upsertGoogleUser(profile) {
        const result = await authRequest("google", { profile: profile || {} });
        if (result.ok) {
            setStoredSession(result.token, result.user);
        }
        return result;
    }

    async function updateProfile({ name, role, avatarUrl }) {
        const token = getStoredToken();
        if (!token) {
            return { ok: false, message: "You need to be logged in." };
        }

        const result = await authRequest("update_profile", {
            token,
            name: (name || "").trim(),
            role: role || "resident",
            avatarUrl: (avatarUrl || "").trim()
        });
        if (result.ok) {
            const currentUser = getSessionUser() || {};
            const mergedUser = sanitizeUser({
                ...currentUser,
                ...(result.user || {}),
                name: (name || "").trim() || (result.user && result.user.name) || currentUser.name || "",
                role: role || (result.user && result.user.role) || currentUser.role || "resident",
                avatarUrl: (avatarUrl || "").trim() || (result.user && result.user.avatarUrl) || currentUser.avatarUrl || ""
            });
            setStoredSession(token, mergedUser);
            result.user = mergedUser;
        }
        return result;
    }

    async function updatePassword({ currentPassword, newPassword }) {
        const token = getStoredToken();
        if (!token) {
            return { ok: false, message: "You need to be logged in." };
        }

        const result = await authRequest("update_password", {
            token,
            currentPassword: currentPassword || "",
            newPassword: newPassword || ""
        });
        if (result.ok) {
            setStoredSession(token, result.user);
        }
        return result;
    }

    async function getFullUserById(userId) {
        const token = getStoredToken();
        if (!token || !userId) return null;
        const result = await authRequest("get_user", { token, userId });
        return result.ok ? sanitizeUser(result.user) : null;
    }

    async function listReports() {
        const token = getStoredToken();
        if (!token) {
            return { ok: false, message: "You need to be logged in." };
        }
        return authRequest("list_reports", { token });
    }

    async function getSessionUserCached(maxAgeMs = 15 * 60 * 1000) {
        const cached = getSessionUser();
        if (cached) return cached;
        return refreshSession({ force: false, maxAgeMs });
    }

    function logout() {
        const token = getStoredToken();
        clearStoredSession();
        if (!token) return;

        const logoutUrl = resolvedAuthApiUrl || getAuthApiCandidates()[0] || "/auth";
        fetch(logoutUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "logout", token })
        }).catch((error) => {
            console.error("Logout request failed", error);
        });
    }

    function getInitials(name, email) {
        const source = (name || email || "?").trim();
        const words = source.split(/\s+/).filter(Boolean);
        if (words.length === 1) {
            return words[0].slice(0, 2).toUpperCase();
        }
        return (words[0][0] + words[1][0]).toUpperCase();
    }

    window.Auth = {
        signUp,
        login,
        logout,
        getSessionUser,
        getSessionUserCached,
        refreshSession,
        getSessionToken,
        getFullUserById,
        listReports,
        getInitials,
        upsertGoogleUser,
        updateProfile,
        updatePassword
    };
})();
