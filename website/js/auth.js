(function () {
    const USERS_KEY = "ecotrack_users_v1";
    const SESSION_KEY = "ecotrack_session_v1";
    const COOKIE_NAME = "ecotrack_auth";

    function normalizeEmail(email) {
        return (email || "").trim().toLowerCase();
    }

    function loadUsers() {
        try {
            const raw = localStorage.getItem(USERS_KEY);
            return raw ? JSON.parse(raw) : [];
        } catch (error) {
            console.error("Unable to read users from storage", error);
            return [];
        }
    }

    function saveUsers(users) {
        localStorage.setItem(USERS_KEY, JSON.stringify(users));
    }

    function sanitizeUser(user) {
        if (!user) return null;
        return {
            id: user.id,
            name: user.name,
            email: user.email,
            provider: user.provider || "local"
        };
    }

    function setAuthCookie(isAuthenticated) {
        if (!isAuthenticated) {
            document.cookie = `${COOKIE_NAME}=; path=/; max-age=0; SameSite=Lax`;
            return;
        }
        const ttlSeconds = 60 * 60 * 24 * 14;
        document.cookie = `${COOKIE_NAME}=1; path=/; max-age=${ttlSeconds}; SameSite=Lax`;
    }

    function createSession(user) {
        const session = {
            user: sanitizeUser(user),
            startedAt: new Date().toISOString()
        };
        localStorage.setItem(SESSION_KEY, JSON.stringify(session));
        setAuthCookie(true);
        return session.user;
    }

    function getSessionUser() {
        try {
            const raw = localStorage.getItem(SESSION_KEY);
            if (!raw) return null;
            const parsed = JSON.parse(raw);
            return parsed && parsed.user ? parsed.user : null;
        } catch (error) {
            console.error("Unable to parse session", error);
            return null;
        }
    }

    function logout() {
        localStorage.removeItem(SESSION_KEY);
        setAuthCookie(false);
    }

    function getInitials(name, email) {
        const source = (name || email || "?").trim();
        const words = source.split(/\s+/).filter(Boolean);
        if (words.length === 1) {
            return words[0].slice(0, 2).toUpperCase();
        }
        return (words[0][0] + words[1][0]).toUpperCase();
    }

    function signUp({ name, email, password }) {
        const cleanName = (name || "").trim();
        const cleanEmail = normalizeEmail(email);
        const cleanPassword = (password || "").trim();

        if (!cleanName) {
            return { ok: false, message: "Name is required." };
        }
        if (!cleanEmail || !cleanEmail.includes("@")) {
            return { ok: false, message: "Valid email is required." };
        }
        if (cleanPassword.length < 8) {
            return { ok: false, message: "Password must be at least 8 characters." };
        }

        const users = loadUsers();
        const existing = users.find(u => normalizeEmail(u.email) === cleanEmail);
        if (existing) {
            return { ok: false, message: "An account with this email already exists." };
        }

        const user = {
            id: crypto.randomUUID ? crypto.randomUUID() : String(Date.now()),
            name: cleanName,
            email: cleanEmail,
            password: cleanPassword,
            provider: "local",
            createdAt: new Date().toISOString()
        };

        users.push(user);
        saveUsers(users);
        createSession(user);
        return { ok: true, user: sanitizeUser(user) };
    }

    function login({ email, password }) {
        const cleanEmail = normalizeEmail(email);
        const cleanPassword = (password || "").trim();
        const users = loadUsers();
        const user = users.find(
            u => normalizeEmail(u.email) === cleanEmail && u.password === cleanPassword
        );
        if (!user) {
            return { ok: false, message: "Incorrect email or password." };
        }

        createSession(user);
        return { ok: true, user: sanitizeUser(user) };
    }

    function upsertGoogleUser(profile) {
        if (!profile || !profile.email) {
            return { ok: false, message: "Google profile missing email." };
        }

        const cleanEmail = normalizeEmail(profile.email);
        const users = loadUsers();
        let user = users.find(u => normalizeEmail(u.email) === cleanEmail);

        if (!user) {
            user = {
                id: profile.sub || (crypto.randomUUID ? crypto.randomUUID() : String(Date.now())),
                name: profile.name || cleanEmail.split("@")[0],
                email: cleanEmail,
                password: "",
                provider: "google",
                createdAt: new Date().toISOString()
            };
            users.push(user);
            saveUsers(users);
        }

        createSession(user);
        return { ok: true, user: sanitizeUser(user) };
    }

    window.Auth = {
        signUp,
        login,
        logout,
        getSessionUser,
        getInitials,
        upsertGoogleUser
    };
})();
