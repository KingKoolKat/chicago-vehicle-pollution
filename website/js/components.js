function createNavigation() {
    const isHomePage = window.location.pathname === '/' || window.location.pathname.endsWith('/index.html');
    const isSubPage = !isHomePage;
    const dashboardLink = isHomePage ? "javascript:void(0)" : "../?section=top";
    const emissionsLink = isHomePage ? "javascript:void(0)" : "../?section=heatmap";
    const uploadLink = "../upload/";
    const chatLink = isHomePage ? "javascript:void(0)" : "../?section=chat";
    const profileLink = isSubPage ? "../profile/" : "profile/";
    const loginLink = isSubPage ? "../login/" : "login/";
    const currentUser = window.Auth ? window.Auth.getSessionUser() : null;
    const initials = currentUser ? window.Auth.getInitials(currentUser.name, currentUser.email) : "";
    const avatarHtml = currentUser && currentUser.avatarUrl
        ? `<img src="${currentUser.avatarUrl}" alt="Profile" class="w-8 h-8 rounded-full object-cover border border-green-300">`
        : `<span class="w-8 h-8 rounded-full bg-green-300 flex items-center justify-center text-xs font-bold">${initials}</span>`;
    const uploadAction = isHomePage
        ? 'onclick="scrollToSection(\'upload\')" href="javascript:void(0)"'
        : `href="${uploadLink}"`;

    const authControls = currentUser
        ? `
            <div class="relative" id="userMenuContainer">
                <button id="userMenuTrigger" type="button" class="flex items-center gap-2 px-2 py-1 rounded-lg border border-green-400/30 bg-blue-200/50 hover:bg-blue-300/70 transition-all">
                    ${avatarHtml}
                    <span class="hidden sm:inline text-sm max-w-28 truncate">${currentUser.name || currentUser.email}</span>
                    <i class="fas fa-chevron-down text-xs"></i>
                </button>
                <div id="userMenuDropdown" class="hidden absolute right-0 mt-2 w-44 rounded-xl glass-panel border border-gray-700 shadow-lg p-2 z-50">
                    <a href="${profileLink}" class="block px-3 py-2 rounded-lg text-sm hover:bg-gray-200">
                        <i class="fas fa-user-gear mr-2"></i>Profile
                    </a>
                    <a ${uploadAction} class="block px-3 py-2 rounded-lg text-sm hover:bg-gray-200">
                        <i class="fas fa-upload mr-2"></i>Upload
                    </a>
                    <button id="logoutBtn" type="button" class="w-full text-left px-3 py-2 rounded-lg text-sm hover:bg-gray-200 text-red-500">
                        <i class="fas fa-right-from-bracket mr-2"></i>Log Out
                    </button>
                </div>
            </div>
        `
        : `<a href="${loginLink}" class="px-3 py-1 rounded-lg bg-green-300 font-semibold hover:shadow-lg transition-all" data-i18n="nav.login">Login</a>`;

    return `
    <nav class="fixed w-full z-50 glass-panel">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="flex items-center justify-between h-[68px] gap-3">

                <div class="absolute left-5 top-1/2 -translate-y-1/2 flex items-center gap-4 text-sm whitespace-nowrap">
                    <img onclick="window.location.href='${isHomePage ? './?section=top' : '../?section=top'}'" src="${isHomePage ? 'image/favicon.png' : '../image/favicon.png'}" alt="VanData Logo" class="w-[84px] h-[84px] object-contain" style="cursor: pointer;">
                </div>

                <div class="absolute right-5 top-1/2 -translate-y-1/2 flex items-center gap-4 text-sm whitespace-nowrap">
                    <a href="${dashboardLink}" ${isHomePage ? 'onclick="scrollToSection(\'top\')"' : ''} class="hover:text-green-400 px-2 sm:px-3 py-2 font-medium" data-i18n="nav.dashboard">Dashboard</a>
                    <a href="${emissionsLink}" ${isHomePage ? 'onclick="scrollToSection(\'heatmap\')"' : ''} class="hover:text-green-400 px-2 sm:px-3 py-2 font-medium" data-i18n="nav.heatmap">Heat Map</a>
                    <a href="${chatLink}" ${isHomePage ? 'onclick="scrollToSection(\'chat\')"' : ''} class="hover:text-green-400 px-2 sm:px-3 py-2 font-medium" data-i18n="nav.chat">AI Assistant</a>

                    <!-- Language Selector inside nav -->
                    <select id="languageSelect"
                        class="glass-panel px-3 py-1 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-green-400 cursor-pointer">
                        <option value="en">English</option>
                        <option value="es">Español</option>
                        <option value="zh">中文</option>
                    </select>
                    ${authControls}
                </div>

            </div>
        </div>
    </nav>`;
}
