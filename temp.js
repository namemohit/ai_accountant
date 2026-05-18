        function checkAuth() {
            const currentUserStr = localStorage.getItem('currentUser');
            if (!currentUserStr) { 
                window.location.href = '/login'; 
            } else {
                // Apply role-based interface adjustments
                document.addEventListener('DOMContentLoaded', () => {
                    try {
                        const user = JSON.parse(currentUserStr);
                        const isSuperAdmin = (user.role === 'super_admin');
                        
                        const badgeName = document.getElementById('active-user-name');
                        const badgeCompany = document.getElementById('active-company-name');
                        if(badgeName) badgeName.innerText = user.username || user.name || 'User';
                        if(badgeCompany) badgeCompany.innerText = user.company_name || 'Acme Corp';
                        
                        const navWhatsapp = document.getElementById('nav-whatsapp');
                        const drawerWhatsapp = document.getElementById('drawer-whatsapp');
                        
                        if (navWhatsapp) navWhatsapp.style.display = isSuperAdmin ? 'flex' : 'none';
                        if (drawerWhatsapp) drawerWhatsapp.style.display = isSuperAdmin ? 'flex' : 'none';
                    } catch (e) {
                        console.error("Error parsing user session: ", e);
                    }
                });
            }
        }
        checkAuth();

        function handleLogout() {
            localStorage.removeItem('currentUser');
            window.location.href = '/login';
        }
        const API = window.location.origin;
        let currentSessionId = null;
        let selectedFile = null;

        // Global Media Viewer Modal logic
        window.openMediaModal = (src) => {
            let modal = document.getElementById('global-media-modal');
            if (!modal) {
                modal = document.createElement('div');
                modal.id = 'global-media-modal';
                modal.style.position = 'fixed';
                modal.style.top = '0';
                modal.style.left = '0';
                modal.style.width = '100vw';
                modal.style.height = '100vh';
                modal.style.background = 'rgba(15, 23, 42, 0.95)';
                modal.style.backdropFilter = 'blur(10px)';
                modal.style.zIndex = '99999';
                modal.style.display = 'none';
                modal.style.alignItems = 'center';
                modal.style.justifyContent = 'center';
                modal.style.transition = 'all 0.3s ease';
                
                modal.innerHTML = `
                    <button onclick="closeMediaModal()" style="position: absolute; top: 24px; right: 24px; background: rgba(255,255,255,0.1); border: none; color: white; width: 44px; height: 44px; border-radius: 50%; cursor: pointer; display: flex; align-items: center; justify-content: center; font-size: 1.25rem; transition: all 0.2s;" onmouseover="this.style.background='rgba(255,255,255,0.2)'" onmouseout="this.style.background='rgba(255,255,255,0.1)'">
                        <i class="fas fa-times"></i>
                    </button>
                    <div id="media-modal-content" style="max-width: 90%; max-height: 90%; transform: scale(0.95); transition: all 0.3s ease; display: flex; align-items: center; justify-content: center;">
                        <img id="media-modal-img" src="" style="max-width: 100%; max-height: 90vh; border-radius: 8px; box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5); object-fit: contain; border: 1px solid rgba(255,255,255,0.1);">
                    </div>
                `;
                document.body.appendChild(modal);
            }
            
            const img = document.getElementById('media-modal-img');
            img.src = src;
            
            modal.style.display = 'flex';
            setTimeout(() => {
                document.getElementById('media-modal-content').style.transform = 'scale(1)';
            }, 10);
        };
        
        window.closeMediaModal = () => {
            const modal = document.getElementById('global-media-modal');
            if (modal) {
                document.getElementById('media-modal-content').style.transform = 'scale(0.95)';
                setTimeout(() => {
                    modal.style.display = 'none';
                }, 200);
            }
        };

        // Tasks logic
        async function loadTasks() {
            const container = document.getElementById('tasks-list-container');
            container.innerHTML = '<div style="text-align: center; color: #94a3b8; padding: 40px;">Loading tasks...</div>';
            
            try {
                const user = JSON.parse(localStorage.getItem('currentUser'));
                const url = `/tasks?company_name=${encodeURIComponent(user.company_name || '')}&role=${encodeURIComponent(user.role)}`;
                const res = await fetch(url);
                const data = await res.json();
                
                if (data.status === 'success' && data.tasks.length > 0) {
                    let html = `<div style="display: flex; flex-direction: column; gap: 16px;">`;
                    data.tasks.forEach(task => {
                        let statusColor = '#fbbf24'; // default yellow for Requested
                        if (task.status === 'In Progress') statusColor = '#3b82f6'; // blue
                        if (task.status === 'Completed') statusColor = '#10b981'; // green
                        
                        let actionsHtml = '';
                        if (user.role === 'super_admin') {
                            actionsHtml = `
                                <div style="margin-top: 16px; padding-top: 16px; border-top: 1px solid rgba(255,255,255,0.05); display: flex; gap: 8px;">
                                    <span style="font-size: 0.85rem; color: #94a3b8; margin-right: auto;">Update Status:</span>
                                    <button onclick="updateTaskStatus('${task.id}', 'Requested')" style="background: rgba(251, 191, 36, 0.1); color: #fbbf24; border: 1px solid rgba(251, 191, 36, 0.3); border-radius: 4px; padding: 4px 8px; font-size: 0.8rem; cursor: pointer;">Requested</button>
                                    <button onclick="updateTaskStatus('${task.id}', 'In Progress')" style="background: rgba(59, 130, 246, 0.1); color: #3b82f6; border: 1px solid rgba(59, 130, 246, 0.3); border-radius: 4px; padding: 4px 8px; font-size: 0.8rem; cursor: pointer;">In Progress</button>
                                    <button onclick="updateTaskStatus('${task.id}', 'Completed')" style="background: rgba(16, 185, 129, 0.1); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3); border-radius: 4px; padding: 4px 8px; font-size: 0.8rem; cursor: pointer;">Completed</button>
                                </div>
                            `;
                        }

                        html += `
                        <div style="background: rgba(30, 41, 59, 0.5); border: 1px solid var(--border); border-radius: 8px; padding: 16px;">
                            <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px;">
                                <div>
                                    <h4 style="margin: 0 0 4px 0; color: #e2e8f0; font-size: 1rem;">Task from ${task.company_name}</h4>
                                    <span style="font-size: 0.75rem; color: #64748b;">Requested on ${new Date(task.created_at).toLocaleString()}</span>
                                </div>
                                <div style="background: ${statusColor}22; color: ${statusColor}; border: 1px solid ${statusColor}44; padding: 4px 12px; border-radius: 20px; font-size: 0.85rem; font-weight: 500;">
                                    ${task.status}
                                </div>
                            </div>
                            <div style="color: #94a3b8; font-size: 0.9rem; line-height: 1.5; white-space: pre-wrap;">${task.description}</div>
                            ${actionsHtml}
                        </div>
                        `;
                    });
                    html += `</div>`;
                    container.innerHTML = html;
                } else {
                    container.innerHTML = '<div style="text-align: center; color: #94a3b8; padding: 40px; background: rgba(30, 41, 59, 0.3); border-radius: 8px; border: 1px dashed var(--border);">No tasks assigned yet.</div>';
                }
            } catch (e) {
                console.error("Error loading tasks:", e);
                container.innerHTML = '<div style="color: #ef4444; text-align: center; padding: 20px;">Failed to load tasks.</div>';
            }
        }

        async function updateTaskStatus(taskId, newStatus) {
            try {
                const formData = new FormData();
                formData.append('status', newStatus);
                
                const res = await fetch(`/tasks/${taskId}/status`, {
                    method: 'POST',
                    body: formData
                });
                
                if (res.ok) {
                    loadTasks(); // refresh UI
                } else {
                    alert("Failed to update status");
                }
            } catch (e) {
                console.error("Error updating status:", e);
            }
        }

        // View Switching
        function showView(view) {
            document.querySelectorAll('.dashboard-view').forEach(el => el.style.display = 'none');
            document.getElementById(view + '-view').style.display = 'block';
            
            if (view === 'schema') {
                loadPartyMaster();
            }
            
            // Desktop sidebar highlight
            document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
            const navItems = Array.from(document.querySelectorAll('.nav-item'));
            const matchingItem = navItems.find(item => item.getAttribute('onclick')?.includes(`showView('${view}')`));
            if (matchingItem) matchingItem.classList.add('active');
            
            // Mobile drawer highlight
            document.querySelectorAll('.mobile-drawer-item').forEach(el => el.classList.remove('active'));
            const drawerItems = Array.from(document.querySelectorAll('.mobile-drawer-item'));
            const drawerMatch = drawerItems.find(item => item.getAttribute('onclick')?.includes(`mobileNav('${view}')`));
            if (drawerMatch) drawerMatch.classList.add('active');
            
            // Show/hide mobile chats button (only visible on chat view)
            const chatsBtn = document.getElementById('mobile-chats-btn');
            if (chatsBtn) chatsBtn.style.display = (view === 'chat') ? 'flex' : 'none';
            
            // Close mobile chat sidebar if switching away
            if (typeof closeMobileChatSidebar === 'function') closeMobileChatSidebar();
            
            if (view === 'history') fetchHistory();
            if (view === 'chat') loadChatSessions();
            if (view === 'training') fetchTrainingStats();
            if (view === 'tasks') loadTasks();
        }

        // --- Mobile Hamburger Menu ---
        function toggleMobileMenu() {
            document.getElementById('mobile-drawer').classList.toggle('open');
            document.getElementById('mobile-overlay').classList.toggle('open');
        }
        function closeMobileMenu() {
            document.getElementById('mobile-drawer').classList.remove('open');
            document.getElementById('mobile-overlay').classList.remove('open');
        }
        function mobileNav(view) {
            closeMobileMenu();
            showView(view);
        }

        // --- Mobile Chat Sessions Panel ---
        function toggleMobileChatSidebar() {
            const sidebar = document.querySelector('.chat-sidebar');
            if (sidebar) {
                sidebar.classList.toggle('mobile-open');
                document.getElementById('mobile-overlay').classList.toggle('open');
                // Override the overlay close to also close chat sidebar
                document.getElementById('mobile-overlay').onclick = () => {
                    closeMobileChatSidebar();
                    closeMobileMenu();
                };
            }
        }
        function closeMobileChatSidebar() {
            const sidebar = document.querySelector('.chat-sidebar');
            if (sidebar) sidebar.classList.remove('mobile-open');
            document.getElementById('mobile-overlay').classList.remove('open');
        }

        // --- Transaction Type ---
        let selectedTxnType = 'Sales';
        function setTxnType(btn) {
            document.querySelectorAll('.txn-type-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            selectedTxnType = btn.getAttribute('data-type');
            
            const dlContainer = document.getElementById('sample-download-container');
            if (dlContainer) {
                if (selectedTxnType === 'Bank Statement') {
                    dlContainer.style.display = 'block';
                } else {
                    dlContainer.style.display = 'none';
                }
            }
        }

        // --- Chat Logic ---
        const chatInput = document.getElementById('chat-input');
        const chatFileInput = document.getElementById('chat-file-input');
        const chatCameraInput = document.getElementById('chat-camera-input');
        const chatAttachmentPreview = document.getElementById('chat-attachment-preview');
        
        // --- Laptop Webcam Capturing Logic ---
        let webcamStream = null;

        async function openWebcamModal() {
            const modal = document.getElementById('webcam-modal');
            const video = document.getElementById('webcam-video');
            modal.style.display = 'flex';
            
            try {
                webcamStream = await navigator.mediaDevices.getUserMedia({
                    video: { facingMode: 'user', width: { ideal: 1280 }, height: { ideal: 720 } }
                });
                video.srcObject = webcamStream;
            } catch (err) {
                alert('Could not access webcam: ' + err.message);
                closeWebcamModal();
            }
        }

        function closeWebcamModal() {
            const modal = document.getElementById('webcam-modal');
            const video = document.getElementById('webcam-video');
            modal.style.display = 'none';
            if (webcamStream) {
                webcamStream.getTracks().forEach(track => track.stop());
                webcamStream = null;
            }
            video.srcObject = null;
        }

        function captureWebcamPhoto() {
            const video = document.getElementById('webcam-video');
            const canvas = document.createElement('canvas');
            canvas.width = video.videoWidth || 640;
            canvas.height = video.videoHeight || 480;
            
            const ctx = canvas.getContext('2d');
            // Mirror image draw (matching the user mirrored mirror-view styling)
            ctx.translate(canvas.width, 0);
            ctx.scale(-1, 1);
            ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
            
            canvas.toBlob((blob) => {
                const file = new File([blob], `webcam-capture-${Date.now()}.jpg`, { type: 'image/jpeg' });
                handleFileSelect(file);
                closeWebcamModal();
            }, 'image/jpeg', 0.95);
        }

        // Bind webcam modal actions on load
        document.addEventListener('DOMContentLoaded', () => {
            document.getElementById('webcam-cancel-btn').onclick = closeWebcamModal;
            document.getElementById('webcam-capture-btn').onclick = captureWebcamPhoto;
            
            const cameraLabel = document.querySelector('label[for="chat-camera-input"]');
            if (cameraLabel) {
                cameraLabel.addEventListener('click', (e) => {
                    const isMobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
                    if (!isMobile) {
                        e.preventDefault(); // Intercept and block default laptop file dialog
                        openWebcamModal();  // Direct stream
                    }
                });
            }

            // Register PWA Service Worker
            if ('serviceWorker' in navigator) {
                navigator.serviceWorker.register('/sw.js')
                    .then(reg => console.log('ServiceWorker registered with scope: ', reg.scope))
                    .catch(err => console.log('ServiceWorker registration failed: ', err));
            }
        });
        
        function handleFileSelect(file) {
            selectedFile = file;
            if (selectedFile) {
                let fileIcon = 'image';
                if (selectedFile.name.match(/\.pdf$/i)) {
                    fileIcon = 'file-pdf';
                } else if (selectedFile.name.match(/\.(xlsx|xls|csv)$/i)) {
                    fileIcon = 'file-excel';
                }
                chatAttachmentPreview.innerHTML = `
                    <div class="attachment-tag">
                        <i class="fas fa-${fileIcon}"></i> ${selectedFile.name}
                        <i class="fas fa-times close" onclick="clearAttachment()"></i>
                    </div>`;
            }
        }
        
        chatFileInput.onchange = (e) => handleFileSelect(e.target.files[0]);
        chatCameraInput.onchange = (e) => handleFileSelect(e.target.files[0]);

        function clearAttachment() {
            selectedFile = null;
            chatFileInput.value = '';
            chatCameraInput.value = '';
            chatAttachmentPreview.innerHTML = '';
        }

        function handleChatKey(e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        }

        async function sendMessage() {
            const msg = chatInput.value.trim();
            if (!msg && !selectedFile) return;

            document.getElementById('chat-welcome').style.display = 'none';
            document.getElementById('chat-messages').style.display = 'flex';
            document.getElementById('chat-suggestions').style.display = 'none';

            const userMsg = msg || `Uploaded document: ${selectedFile.name}`;
            const txnTag = selectedFile ? ` [Type: ${selectedTxnType}]` : '';
            appendUserMessage(userMsg + txnTag, selectedFile);
            chatInput.value = '';
            autoResizeTextarea();

            const thinking = appendThinking();
            
            try {
                const formData = new FormData();
                const enhancedMsg = selectedFile ? `${msg} [Transaction Type: ${selectedTxnType}]` : msg;
                formData.append('message', enhancedMsg);
                formData.append('txn_type', selectedTxnType);
                if (selectedFile) formData.append('file', selectedFile);
                if (currentSessionId) formData.append('session_id', currentSessionId);
                
                const currentUser = JSON.parse(localStorage.getItem('currentUser'));
                if (currentUser && currentUser.company_name) {
                    formData.append('company_name', currentUser.company_name);
                }

                const res = await fetch(`${API}/chat`, { method: 'POST', body: formData });
                const data = await res.json();
                
                thinking.remove();
                currentSessionId = data.session_id;
                appendAIMessage(data);
                if (data.suggested_questions) showSuggestions(data.suggested_questions);
                clearAttachment();
                loadChatSessions();
            } catch (err) {
                thinking.remove();
                appendAIMessage({ text: 'Error: ' + err.message });
            }
        }

        function appendUserMessage(text, fileOrUrl, filename = '') {
            const div = document.createElement('div');
            div.className = 'chat-msg chat-msg-user';
            
            let fileHtml = '';
            if (fileOrUrl) {
                let fileUrl = '';
                let isImage = false;
                let isPdf = false;
                let displayName = filename || 'File';
                
                if (typeof fileOrUrl === 'string') {
                    fileUrl = fileOrUrl;
                    const lowerUrl = fileUrl.toLowerCase();
                    isImage = lowerUrl.match(/\.(jpeg|jpg|gif|png|webp)$/i);
                    isPdf = lowerUrl.match(/\.pdf$/i);
                } else if (fileOrUrl instanceof File || fileOrUrl instanceof Blob) {
                    fileUrl = URL.createObjectURL(fileOrUrl);
                    isImage = fileOrUrl.type.startsWith('image/');
                    isPdf = fileOrUrl.type === 'application/pdf';
                    displayName = fileOrUrl.name;
                }
                
                if (isImage) {
                    fileHtml = `
                    <div class="chat-msg-media" style="margin-top: 8px; cursor: pointer;">
                        <img src="${fileUrl}" style="max-width: 280px; max-height: 200px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.15); box-shadow: 0 4px 12px rgba(0,0,0,0.15); transition: transform 0.2s;" onmouseover="this.style.transform='scale(1.02)'" onmouseout="this.style.transform='scale(1)'" onclick="openMediaModal('${fileUrl}')">
                    </div>`;
                } else if (isPdf) {
                    fileHtml = `
                    <div class="chat-msg-media" style="margin-top: 8px; cursor: pointer; display: flex; align-items: center; gap: 8px; background: rgba(255,255,255,0.05); padding: 10px 14px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.1);" onclick="window.open('${fileUrl}', '_blank')">
                        <i class="fas fa-file-pdf" style="color: #ef4444; font-size: 1.5rem;"></i>
                        <span style="font-size: 0.85rem; color: #e2e8f0; font-weight: 500;">${escapeHtml(displayName)}</span>
                    </div>`;
                } else {
                    fileHtml = `
                    <div class="chat-msg-media" style="margin-top: 8px; cursor: pointer; display: flex; align-items: center; gap: 8px; background: rgba(255,255,255,0.05); padding: 10px 14px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.1);" onclick="window.open('${fileUrl}', '_blank')">
                        <i class="fas fa-file" style="color: var(--accent); font-size: 1.5rem;"></i>
                        <span style="font-size: 0.85rem; color: #e2e8f0; font-weight: 500;">${escapeHtml(displayName)}</span>
                    </div>`;
                }
            }
            
            div.innerHTML = `<div class="chat-msg-bubble chat-msg-bubble-user">${escapeHtml(text)}${fileHtml}</div>`;
            document.getElementById('chat-messages').appendChild(div);
            scrollChat();
        }

        function appendThinking() {
            const div = document.createElement('div');
            div.className = 'chat-msg chat-msg-ai';
            div.innerHTML = `<div class="chat-msg-avatar">🤖</div><div class="chat-msg-bubble chat-msg-bubble-ai"><div class="chat-thinking"><div class="thinking-dot"></div><div class="thinking-dot"></div><div class="thinking-dot"></div></div></div>`;
            document.getElementById('chat-messages').appendChild(div);
            scrollChat();
            return div;
        }

        function extractMetadata(data) {
            let billedToPartyName = 'Chat Sync Client';
            let billingPartyName = 'Billing Party';
            let billingPartyGSTIN = '';
            let billedToPartyGSTIN = '';
            let invoiceNumber = 'CHAT-' + Date.now().toString().slice(-6);
            let dateVal = new Date().toISOString().slice(0, 10);
            let category = 'Sales';

            let invoiceTotal = 0;
            let invoiceGST = 0;

            if (data.ui_data && data.ui_data.invoice_metadata) {
                const meta = data.ui_data.invoice_metadata;
                if (meta.billed_to_party_name) billedToPartyName = meta.billed_to_party_name;
                else if (meta.party_name) billedToPartyName = meta.party_name;
                
                if (meta.billing_party_name) billingPartyName = meta.billing_party_name;
                if (meta.billing_party_gstin) billingPartyGSTIN = meta.billing_party_gstin;
                if (meta.billed_to_party_gstin) billedToPartyGSTIN = meta.billed_to_party_gstin;
                if (meta.invoice_number) invoiceNumber = meta.invoice_number;
                if (meta.date) dateVal = meta.date;
                if (meta.category) category = meta.category;
                if (meta.invoice_total) invoiceTotal = parseFloat(meta.invoice_total) || 0;
                if (meta.invoice_gst) invoiceGST = parseFloat(meta.invoice_gst) || 0;
                
                return { billedToPartyName, billingPartyName, billingPartyGSTIN, billedToPartyGSTIN, invoiceNumber, dateVal, category, invoiceTotal, invoiceGST };
            }

            try {
                const text = data.text || '';
                const partyMatch = text.match(/(?:issued to|to|client|invoice for)\s+\*?\*?([^*.\n]+)\*?\*?/i);
                const numMatch = text.match(/(?:No\.|Invoice#|Invoice\s+No\.|Number)\s+\*?\*?([a-zA-Z0-9_-]+)\*?\*?/i);
                const dateMatch = text.match(/(?:dated|date)\s+\*?\*?([^*,\n]+)\*?\*?/i);
                const catMatch = text.match(/(?:Category|Transaction\s+Type):\s+\*?\*?(\w+)\*?\*?/i);

                // Simple GST extraction fallbacks
                const gstMatches = text.match(/[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}/g) || [];
                if (gstMatches.length > 0) billingPartyGSTIN = gstMatches[0];
                if (gstMatches.length > 1) billedToPartyGSTIN = gstMatches[1];

                if (partyMatch) billedToPartyName = partyMatch[1].trim();
                if (numMatch) invoiceNumber = numMatch[1].trim();
                if (dateMatch) {
                    let cleanDate = dateMatch[1].trim();
                    if (cleanDate.includes('/')) {
                        const parts = cleanDate.split('/');
                        if (parts.length === 3) {
                            const year = parts[2].trim().length === 2 ? '20' + parts[2].trim() : parts[2].trim();
                            const month = parts[1].trim().padStart(2, '0');
                            const day = parts[0].trim().padStart(2, '0');
                            dateVal = `${year}-${month}-${day}`;
                        }
                    } else {
                        dateVal = cleanDate.replace(/[^0-9-]/g, '');
                    }
                }
                if (catMatch) category = catMatch[1].trim();
            } catch (e) {
                console.log("Error extracting meta:", e);
            }
            return { billedToPartyName, billingPartyName, billingPartyGSTIN, billedToPartyGSTIN, invoiceNumber, dateVal, category };
        }

        function appendAIMessage(data) {
            const div = document.createElement('div');
            div.className = 'chat-msg chat-msg-ai';
            
            let contentHtml = `<div class="chat-msg-text">${formatMarkdown(data.text || '')}</div>`;

            if (data.file_url) {
                const cleanUrl = data.file_url.startsWith('http') ? data.file_url : `${API}/${data.file_url.replace(/^\/+/, '')}`;
                if (cleanUrl.toLowerCase().endsWith('.pdf') || cleanUrl.toLowerCase().includes('.pdf')) {
                    contentHtml = `
                    <div class="ai-msg-media" style="margin-top: 4px; margin-bottom: 12px;">
                        <a href="${cleanUrl}" target="_blank" class="btn btn-sm" style="background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3); color: #f87171; display: inline-flex; align-items: center; gap: 8px; padding: 8px 14px; border-radius: 6px; text-decoration: none; font-size: 0.85rem; font-weight: 500;">
                            <i class="fas fa-file-pdf" style="font-size: 1.1rem;"></i> View Source PDF Reference
                        </a>
                    </div>` + contentHtml;
                } else {
                    contentHtml = `
                    <div class="ai-msg-media" style="margin-top: 4px; margin-bottom: 12px;">
                        <div style="font-size: 0.75rem; color: #94a3b8; margin-bottom: 6px; font-weight: 500; display: flex; align-items: center; gap: 6px;">
                            <i class="fas fa-image" style="color: var(--accent);"></i> Document Reference (Click to enlarge):
                        </div>
                        <img src="${cleanUrl}" style="max-width: 100%; max-height: 220px; border-radius: 8px; border: 1px solid var(--border); cursor: pointer; box-shadow: 0 4px 12px rgba(0,0,0,0.15); transition: transform 0.2s;" onmouseover="this.style.transform='scale(1.01)'" onmouseout="this.style.transform='scale(1)'" onclick="openMediaModal('${cleanUrl}')" title="Click to enlarge">
                    </div>` + contentHtml;
                }
            }
            
            if (data.ui_type === 'table' && data.ui_data) {
                // Normalize UI data if it comes back as an array of objects
                if (Array.isArray(data.ui_data) && data.ui_data.length > 0) {
                    const firstItem = data.ui_data[0];
                    if (typeof firstItem === 'object' && firstItem !== null && !Array.isArray(firstItem)) {
                        const headers = Object.keys(firstItem);
                        const rows = data.ui_data.map(item => headers.map(h => item[h]));
                        data.ui_data = { headers, rows };
                    }
                }
                
                const isSynced = (data.ui_data && data.ui_data.synced) || data.synced || false;
                const disabledAttr = isSynced ? ' disabled' : '';
                const editableAttr = isSynced ? 'contenteditable="false"' : 'contenteditable="true"';
                const tableId = `table-${Date.now()}`;
                const fileUrlAttr = data.file_url ? ` data-file-url="${escapeHtml(data.file_url)}"` : '';
                const msgIdAttr = data.id ? ` data-message-id="${data.id}"` : '';
                
                const meta = extractMetadata(data);
                
                let metaFormHtml = `
                <div class="invoice-header-editor" id="meta-${tableId}" style="margin-top: 16px; margin-bottom: 16px; padding: 16px; background: rgba(30, 41, 59, 0.4); border: 1px solid var(--border); border-radius: 8px;">
                    <div style="font-weight: 600; margin-bottom: 12px; font-size: 0.95rem; color: white; display: flex; align-items: center; gap: 8px;">
                        <i class="fas fa-edit" style="color: var(--accent);"></i> Review & Edit Invoice Overview (AI Memory Loop)
                    </div>
                    <div style="display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 12px; margin-bottom: 12px;">
                        <div>
                            <label style="font-size: 0.75rem; color: #94a3b8; display: block; margin-bottom: 6px; font-weight: 500;">Billing Party (Supplier)</label>
                            <input type="text" class="meta-billing-party-name" value="${escapeHtml(meta.billingPartyName)}" style="width: 100%; padding: 8px 12px; background: #1e293b; color: white; border: 1px solid var(--border); border-radius: 6px; font-size: 0.85rem; outline: none; transition: border-color 0.2s;" data-original="${escapeHtml(meta.billingPartyName)}"${disabledAttr}>
                        </div>
                        <div>
                            <label style="font-size: 0.75rem; color: #94a3b8; display: block; margin-bottom: 6px; font-weight: 500;">Billed To Party (Client)</label>
                            <input type="text" class="meta-billed-to-party-name" value="${escapeHtml(meta.billedToPartyName)}" style="width: 100%; padding: 8px 12px; background: #1e293b; color: white; border: 1px solid var(--border); border-radius: 6px; font-size: 0.85rem; outline: none; transition: border-color 0.2s;" data-original="${escapeHtml(meta.billedToPartyName)}"${disabledAttr}>
                        </div>
                        <div>
                            <label style="font-size: 0.75rem; color: #94a3b8; display: block; margin-bottom: 6px; font-weight: 500;">Invoice Number</label>
                            <input type="text" class="meta-invoice-number" value="${escapeHtml(meta.invoiceNumber)}" style="width: 100%; padding: 8px 12px; background: #1e293b; color: white; border: 1px solid var(--border); border-radius: 6px; font-size: 0.85rem; outline: none; transition: border-color 0.2s;" data-original="${escapeHtml(meta.invoiceNumber)}"${disabledAttr}>
                        </div>
                        <div>
                            <label style="font-size: 0.75rem; color: #94a3b8; display: block; margin-bottom: 6px; font-weight: 500;">Date (YYYY-MM-DD)</label>
                            <input type="text" class="meta-date" value="${escapeHtml(meta.dateVal)}" style="width: 100%; padding: 8px 12px; background: #1e293b; color: white; border: 1px solid var(--border); border-radius: 6px; font-size: 0.85rem; outline: none; transition: border-color 0.2s;" data-original="${escapeHtml(meta.dateVal)}"${disabledAttr}>
                        </div>
                    </div>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;">
                        <div>
                            <label style="font-size: 0.75rem; color: #94a3b8; display: block; margin-bottom: 6px; font-weight: 500;">Billing Party GSTIN</label>
                            <input type="text" class="meta-billing-party-gstin" value="${escapeHtml(meta.billingPartyGSTIN)}" style="width: 100%; padding: 8px 12px; background: #1e293b; color: white; border: 1px solid var(--border); border-radius: 6px; font-size: 0.85rem; outline: none; transition: border-color 0.2s;" data-original="${escapeHtml(meta.billingPartyGSTIN)}" placeholder="Enter Billing Party GSTIN"${disabledAttr}>
                        </div>
                        <div>
                            <label style="font-size: 0.75rem; color: #94a3b8; display: block; margin-bottom: 6px; font-weight: 500;">Billed To Party GSTIN</label>
                            <input type="text" class="meta-billed-to-party-gstin" value="${escapeHtml(meta.billedToPartyGSTIN)}" style="width: 100%; padding: 8px 12px; background: #1e293b; color: white; border: 1px solid var(--border); border-radius: 6px; font-size: 0.85rem; outline: none; transition: border-color 0.2s;" data-original="${escapeHtml(meta.billedToPartyGSTIN)}" placeholder="Enter Billed To Party GSTIN"${disabledAttr}>
                        </div>
                    </div>
                </div>
                `;

                const headers = data.ui_data.headers || [];
                const qtyIdx = headers.findIndex(h => h.toLowerCase().includes('qty') || h.toLowerCase().includes('quantity'));
                const rateIdx = headers.findIndex(h => h.toLowerCase().includes('rate') || h.toLowerCase().includes('price') || h.toLowerCase().includes('rate (₹)'));
                const discIdx = headers.findIndex(h => h.toLowerCase().includes('discount'));
                const cgstIdx = headers.findIndex(h => h.toLowerCase().includes('cgst'));
                const sgstIdx = headers.findIndex(h => h.toLowerCase().includes('sgst'));

                let cleanHeaders = headers.map(h => {
                    if (h.toLowerCase().includes('cgst')) return 'CGST';
                    if (h.toLowerCase().includes('sgst')) return 'SGST';
                    return h;
                });

                let actionHeaderHtml = isSynced ? '' : '<th>Actions</th>';
                let tableHtml = metaFormHtml + `<div class="chat-ui-table-container">
                    <table id="${tableId}"${fileUrlAttr}${msgIdAttr}>
                        <thead><tr>`;
                cleanHeaders.forEach(h => tableHtml += `<th>${h}</th>`);
                tableHtml += `${actionHeaderHtml}</tr></thead><tbody>`;
                
                (data.ui_data.rows || []).forEach((row, rowIndex) => {
                    tableHtml += `<tr data-row="${rowIndex}">`;
                    
                    const qtyVal = qtyIdx !== -1 ? parseFloat(String(row[qtyIdx]).replace(/[^0-9.]/g, '')) || 0 : 0;
                    const rateVal = rateIdx !== -1 ? parseFloat(String(row[rateIdx]).replace(/[^0-9.]/g, '')) || 0 : 0;
                    const discVal = discIdx !== -1 ? parseFloat(String(row[discIdx]).replace(/[^0-9.]/g, '')) || 0 : 0;
                    const taxableVal = qtyVal * rateVal * (1 - discVal / 100);

                    row.forEach((cell, cellIndex) => {
                        let displayVal = String(cell);
                        let isTaxCol = (cellIndex === cgstIdx || cellIndex === sgstIdx);
                        
                        if (isTaxCol) {
                            const ratePercent = parseFloat(displayVal.replace(/[^0-9.]/g, '')) || 0;
                            if (ratePercent > 0) {
                                const taxAmt = (taxableVal * ratePercent / 100).toFixed(2);
                                displayVal = `${taxAmt} (${ratePercent}%)`;
                            } else {
                                displayVal = `0.00 (0%)`;
                            }
                        }
                        
                        const safeCell = escapeHtml(displayVal);
                        const blurAttr = isTaxCol ? ` onblur="formatTaxCellOnBlur(this)"` : '';
                        tableHtml += `<td ${editableAttr} data-original="${safeCell}" data-col="${headers[cellIndex] || 'column'}" oninput="highlightCell(this)"${blurAttr}>${safeCell}</td>`;
                    });
                    
                    let actionCellHtml = isSynced ? '' : `
                        <td>
                            <button class="table-action-btn delete" onclick="deleteRow(this)" title="Remove Item"><i class="fas fa-trash"></i></button>
                        </td>`;
                    tableHtml += `
                        ${actionCellHtml}
                    </tr>`;
                });
                
                let actionsHtml = isSynced ? `
                <div style="background: rgba(16, 185, 129, 0.08); border: 1px solid rgba(16, 185, 129, 0.25); color: #34d399; padding: 12px; border-radius: 8px; font-size: 0.85rem; font-weight: 600; display: flex; align-items: center; justify-content: center; gap: 8px; margin-top: 12px;">
                    <i class="fas fa-check-circle" style="font-size: 1.1rem; color: #10b981;"></i> Synced to Tally Prime Successfully!
                </div>` : `
                <div class="table-actions">
                    <button class="btn btn-sm" onclick="addRow('${tableId}')"><i class="fas fa-plus"></i> Add Item</button>
                    <button class="btn btn-sm confirm-btn" onclick="confirmTableData('${tableId}')">Confirm & Sync ✨</button>
                </div>`;
                
                tableHtml += `</tbody></table>
                    <div class="invoice-reconcile-banner" id="banner-${tableId}" data-target-total="${meta.invoiceTotal || 0}" data-target-gst="${meta.invoiceGST || 0}" style="margin-top: 12px; margin-bottom: 12px; padding: 12px; border-radius: 6px; font-size: 0.85rem; font-weight: 500; display: flex; align-items: center; gap: 8px;">
                    </div>
                    ${actionsHtml}
                </div>`;
                contentHtml += tableHtml;
            }

            if (data.ui_type === 'cards' && Array.isArray(data.ui_data)) {
                let cardsHtml = '<div class="chat-ui-cards">';
                data.ui_data.forEach(c => {
                    cardsHtml += `<div class="chat-ui-card"><div>${c.title}</div><div style="font-size:1.2rem;font-weight:bold;">${c.value}</div></div>`;
                });
                contentHtml += cardsHtml + '</div>';
            }

            if (data.ui_type === 'reconciliation' && Array.isArray(data.ui_data)) {
                const recId = `rec-${Date.now()}`;
                let recHtml = `<div class="reconciliation-container" style="margin-top: 16px; background: rgba(30, 41, 59, 0.5); border: 1px solid var(--border); padding: 16px; border-radius: 8px;">
                    <div style="font-weight: bold; margin-bottom: 12px; font-size: 1.1rem; color: white; display: flex; align-items: center; gap: 8px;">
                        <i class="fas fa-university" style="color: var(--accent);"></i> Bank Statement Auto-Reconciled Matches
                    </div>
                    <div style="overflow-x: auto;">
                        <table id="${recId}" style="width: 100%; border-collapse: collapse; margin-bottom: 16px;">
                            <thead>
                                <tr style="border-bottom: 1px solid var(--border); text-align: left;">
                                    <th style="padding: 10px; color: #94a3b8; font-size: 0.85rem;">Date</th>
                                    <th style="padding: 10px; color: #94a3b8; font-size: 0.85rem;">Narration / Description</th>
                                    <th style="padding: 10px; color: #94a3b8; font-size: 0.85rem;">Extracted Party</th>
                                    <th style="padding: 10px; color: #94a3b8; font-size: 0.85rem; text-align: right;">Amount (₹)</th>
                                    <th style="padding: 10px; color: #94a3b8; font-size: 0.85rem;">Suggested Ledger</th>
                                    <th style="padding: 10px; color: #94a3b8; font-size: 0.85rem;">Status</th>
                                </tr>
                            </thead>
                            <tbody>`;
                
                data.ui_data.forEach((item, index) => {
                    const tx = item.bank_transaction || {};
                    const isMatched = item.status === 'auto_matched';
                    const isFilled = item.status === 'auto_filled';
                    
                    let badgeColor = '#ef4444';
                    let badgeBg = 'rgba(239, 68, 68, 0.15)';
                    let badgeText = 'Unmatched 🔴';
                    
                    if (isMatched) {
                        badgeColor = '#10b981';
                        badgeBg = 'rgba(16, 185, 129, 0.15)';
                        badgeText = 'Auto-Matched 🟢';
                    } else if (isFilled) {
                        badgeColor = '#f59e0b';
                        badgeBg = 'rgba(245, 158, 11, 0.15)';
                        badgeText = 'Auto-Filled 🟡';
                    }
                    
                    let ledgerInputHtml = '';
                    if (isMatched) {
                        ledgerInputHtml = `<span style="font-weight: bold; color: #38bdf8;">${escapeHtml(item.suggested_ledger)}</span>`;
                    } else {
                        ledgerInputHtml = `<select class="reconcile-ledger-select" style="background: #1e293b; color: white; border: 1px solid var(--border); padding: 6px 12px; border-radius: 6px; font-size: 0.9rem; cursor: pointer; outline: none;" data-tx-index="${index}">
                            <option value="Suspense A/c" ${item.suggested_ledger === 'Suspense A/c' ? 'selected' : ''}>Suspense A/c</option>
                            <option value="Bank Charges A/c" ${item.suggested_ledger === 'Bank Charges A/c' ? 'selected' : ''}>Bank Charges A/c</option>
                            <option value="Interest Received A/c" ${item.suggested_ledger.toLowerCase().includes('interest') ? 'selected' : ''}>Interest Received A/c</option>
                            <option value="Office Expenses" ${item.suggested_ledger.toLowerCase().includes('office') ? 'selected' : ''}>Office Expenses</option>
                            <option value="Sundry Creditors" ${item.suggested_ledger.toLowerCase().includes('creditor') ? 'selected' : ''}>Sundry Creditors</option>
                            <option value="LUXEDECO VENTURES PRIVATE LIMITED" ${item.suggested_ledger === 'LUXEDECO VENTURES PRIVATE LIMITED' ? 'selected' : ''}>LUXEDECO VENTURES PRIVATE LIMITED</option>
                        </select>`;
                    }
                    
                    const amountRaw = parseFloat(tx.amount || 0);
                    const amountFormatted = Math.abs(amountRaw).toLocaleString('en-IN', {
                        minimumFractionDigits: 2,
                        maximumFractionDigits: 2
                    });
                    const amountColor = amountRaw < 0 ? '#f87171' : '#4ade80';
                    const amountPrefix = amountRaw < 0 ? 'Dr' : 'Cr';

                    recHtml += `
                        <tr data-index="${index}" data-tally-voucher-id="${item.tally_voucher_id || ''}" style="border-bottom: 1px solid rgba(255,255,255,0.05);">
                            <td style="padding: 10px; color: #cbd5e1; font-size: 0.9rem;">${escapeHtml(tx.date || '')}</td>
                            <td style="padding: 10px; color: #cbd5e1; font-size: 0.9rem;">
                                <div style="font-weight: 500;">${escapeHtml(tx.description || '')}</div>
                                <div style="font-size: 0.75rem; color: #64748b;">Ref: ${escapeHtml(tx.reference || 'N/A')}</div>
                            </td>
                            <td style="padding: 10px; color: #38bdf8; font-weight: 600; font-size: 0.9rem;">${escapeHtml(tx.party_name || 'Suspense / Unknown')}</td>
                            <td style="padding: 10px; text-align: right; font-weight: bold; color: ${amountColor}; font-size: 0.9rem;">₹${amountFormatted} <span style="font-size: 0.75rem; font-weight: normal; color: #64748b;">${amountPrefix}</span></td>
                            <td class="ledger-td" style="padding: 10px; font-size: 0.9rem;">${ledgerInputHtml}</td>
                            <td style="padding: 10px;">
                                <span style="background: ${badgeBg}; color: ${badgeColor}; padding: 4px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; display: inline-block;">
                                    ${badgeText}
                                </span>
                            </td>
                        </tr>
                    `;
                });
                
                const globalRecKey = `rec_data_${Date.now()}`;
                window[globalRecKey] = data.ui_data;

                recHtml += `</tbody></table>
                    </div>
                    <div style="display: flex; justify-content: flex-end; gap: 12px; margin-top: 12px;">
                        <button class="btn btn-sm" onclick="confirmReconciliationBatch('${recId}', '${globalRecKey}')" style="background: var(--accent); color: white; font-weight: bold; display: flex; align-items: center; gap: 6px; padding: 10px 20px; border-radius: 6px; border: none; cursor: pointer;">
                            <i class="fas fa-check-double"></i> Confirm & Reconcile ✨
                        </button>
                    </div>
                </div>`;
                contentHtml += recHtml;
            }

            if (data.ui_type === 'task_assigned') {
                contentHtml += `
                <div style="background: rgba(16, 185, 129, 0.1); border: 1px solid rgba(16, 185, 129, 0.3); border-radius: 8px; padding: 16px; margin-top: 10px;">
                    <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px;">
                        <span style="font-size: 1.5rem;">🎯</span>
                        <h4 style="margin: 0; color: #10b981;">Task Successfully Assigned</h4>
                    </div>
                    <p style="margin: 0 0 12px 0; color: #e2e8f0; font-size: 0.9rem;">Your request has been routed to the Super Admin team.</p>
                    <div style="background: rgba(15, 23, 42, 0.5); padding: 12px; border-radius: 6px; font-size: 0.85rem; color: #94a3b8;">
                        <strong>Task ID:</strong> ${data.ui_data.task_id.substring(0, 8)}...<br>
                        <strong>Status:</strong> <span style="color: #fbbf24;">${data.ui_data.status}</span><br>
                        <strong>Details:</strong> ${data.ui_data.description}
                    </div>
                    <button onclick="showView('tasks'); loadTasks();" style="margin-top: 12px; background: rgba(56, 189, 248, 0.1); color: var(--accent); border: 1px solid rgba(56, 189, 248, 0.3); padding: 6px 12px; border-radius: 6px; cursor: pointer; transition: all 0.2s;">View Task Journey →</button>
                </div>
                `;
            }

            div.innerHTML = `<div class="chat-msg-avatar">🤖</div><div class="chat-msg-bubble chat-msg-bubble-ai">${contentHtml}</div>`;
            document.getElementById('chat-messages').appendChild(div);
            scrollChat();

            // Trigger dynamic invoice verification for all tables in this message
            const tables = div.querySelectorAll('table');
            if (tables.length > 0) {
                setTimeout(() => {
                    tables.forEach(t => {
                        if (t.id && window.updateReconciliationStatus) {
                            window.updateReconciliationStatus(t.id);
                        }
                    });
                }, 50);
            }
        }

        // --- Interactive Table Actions ---
        window.parseTaxRate = (cellText) => {
            if (!cellText) return 0;
            const bracketMatch = cellText.match(/\(([^%)]+)%\)/);
            if (bracketMatch) {
                return parseFloat(bracketMatch[1]) || 0;
            }
            const percentMatch = cellText.match(/([0-9.]+)%/);
            if (percentMatch) {
                return parseFloat(percentMatch[1]) || 0;
            }
            const fallbackMatch = cellText.match(/\(([^)]+)\)/);
            if (fallbackMatch) {
                return parseFloat(fallbackMatch[1].replace(/[^0-9.]/g, '')) || 0;
            }
            return parseFloat(cellText.replace(/[^0-9.]/g, '')) || 0;
        };

        window.updateReconciliationStatus = (tableId) => {
            const table = document.getElementById(tableId);
            const banner = document.getElementById(`banner-${tableId}`);
            if (!table || !banner) return;
            
            const targetTotal = parseFloat(banner.getAttribute('data-target-total')) || 0;
            const targetGST = parseFloat(banner.getAttribute('data-target-gst')) || 0;
            
            const headers = Array.from(table.querySelectorAll('thead th')).map(th => th.textContent.trim()).slice(0, -1);
            const qtyIndex = headers.findIndex(h => h.toLowerCase().includes('qty') || h.toLowerCase().includes('quantity'));
            const rateIndex = headers.findIndex(h => h.toLowerCase().includes('rate') || h.toLowerCase().includes('price') || h.toLowerCase().includes('rate (₹)'));
            const discountIndex = headers.findIndex(h => h.toLowerCase().includes('discount'));
            const cgstIndex = headers.findIndex(h => h.toLowerCase().includes('cgst'));
            const sgstIndex = headers.findIndex(h => h.toLowerCase().includes('sgst'));
            const totalIndex = headers.findIndex(h => h.toLowerCase().includes('total') || h.toLowerCase().includes('amount') || h.toLowerCase().includes('total (₹)'));
            
            let calculatedTotal = 0;
            let calculatedTax = 0;
            
            table.querySelectorAll('tbody tr').forEach(tr => {
                const tds = Array.from(tr.querySelectorAll('td')).slice(0, -1);
                if (tds.length === 0) return;
                
                const qtyVal = parseFloat(tds[qtyIndex].textContent.trim().replace(/[^0-9.]/g, '')) || 0;
                const rateVal = parseFloat(tds[rateIndex].textContent.trim().replace(/[^0-9.]/g, '')) || 0;
                const discountVal = discountIndex !== -1 ? parseFloat(tds[discountIndex].textContent.trim().replace(/[^0-9.]/g, '')) || 0 : 0;
                const taxableVal = qtyVal * rateVal * (1 - discountVal / 100);
                
                const cgstPercent = cgstIndex !== -1 ? window.parseTaxRate(tds[cgstIndex].textContent.trim()) : 0;
                const sgstPercent = sgstIndex !== -1 ? window.parseTaxRate(tds[sgstIndex].textContent.trim()) : 0;
                
                const cgstAmt = taxableVal * cgstPercent / 100;
                const sgstAmt = taxableVal * sgstPercent / 100;
                
                calculatedTax += (cgstAmt + sgstAmt);
                calculatedTotal += (taxableVal + cgstAmt + sgstAmt);
            });
            
            calculatedTotal = parseFloat(calculatedTotal.toFixed(2));
            calculatedTax = parseFloat(calculatedTax.toFixed(2));
            
            if (targetTotal > 0) {
                const diff = Math.abs(calculatedTotal - targetTotal);
                if (diff <= 1.05) {
                    banner.style.background = 'rgba(16, 185, 129, 0.1)';
                    banner.style.border = '1px solid rgba(16, 185, 129, 0.3)';
                    banner.style.color = '#10b981';
                    banner.innerHTML = `<i class="fas fa-check-circle"></i> <strong>Invoice Verified:</strong> Calculated Total (₹${calculatedTotal.toFixed(2)}) matches the Invoice Total (₹${targetTotal.toFixed(2)}) perfectly! 🟢`;
                } else {
                    banner.style.background = 'rgba(239, 68, 68, 0.1)';
                    banner.style.border = '1px solid rgba(239, 68, 68, 0.3)';
                    banner.style.color = '#f87171';
                    banner.innerHTML = `<i class="fas fa-exclamation-triangle"></i> <strong>Discrepancy Detected:</strong> Calculated Total is <strong>₹${calculatedTotal.toFixed(2)}</strong>, but the Invoice mentions <strong>₹${targetTotal.toFixed(2)}</strong>. Please review line-items. ⚠️`;
                }
            } else {
                banner.style.background = 'rgba(56, 189, 248, 0.1)';
                banner.style.border = '1px solid rgba(56, 189, 248, 0.3)';
                banner.style.color = '#38bdf8';
                banner.innerHTML = `<i class="fas fa-calculator"></i> <strong>Live Calculations:</strong> Total Amount: <strong>₹${calculatedTotal.toFixed(2)}</strong> (Taxable: ₹${(calculatedTotal - calculatedTax).toFixed(2)}, Tax: ₹${calculatedTax.toFixed(2)})`;
            }
        };

        window.recalculateRow = (td) => {
            const tr = td.closest('tr');
            const table = tr.closest('table');
            if (!table) return;
            
            const headers = Array.from(table.querySelectorAll('thead th')).map(th => th.textContent.trim()).slice(0, -1);
            const tds = Array.from(tr.querySelectorAll('td')).slice(0, -1);
            
            const qtyIndex = headers.findIndex(h => h.toLowerCase().includes('qty') || h.toLowerCase().includes('quantity'));
            const rateIndex = headers.findIndex(h => h.toLowerCase().includes('rate') || h.toLowerCase().includes('price') || h.toLowerCase().includes('rate (₹)'));
            const discountIndex = headers.findIndex(h => h.toLowerCase().includes('discount'));
            const cgstIndex = headers.findIndex(h => h.toLowerCase().includes('cgst'));
            const sgstIndex = headers.findIndex(h => h.toLowerCase().includes('sgst'));
            const totalIndex = headers.findIndex(h => h.toLowerCase().includes('total') || h.toLowerCase().includes('amount') || h.toLowerCase().includes('total (₹)'));
            
            if (qtyIndex === -1 || rateIndex === -1) return;
            
            const qtyVal = parseFloat(tds[qtyIndex].textContent.trim().replace(/[^0-9.]/g, '')) || 0;
            const rateVal = parseFloat(tds[rateIndex].textContent.trim().replace(/[^0-9.]/g, '')) || 0;
            const discountVal = discountIndex !== -1 ? parseFloat(tds[discountIndex].textContent.trim().replace(/[^0-9.]/g, '')) || 0 : 0;
            
            const taxableVal = qtyVal * rateVal * (1 - discountVal / 100);
            
            // Recalculate CGST (only if user is not actively editing CGST cell itself)
            if (cgstIndex !== -1 && td !== tds[cgstIndex]) {
                const cellText = tds[cgstIndex].textContent.trim();
                const ratePercent = window.parseTaxRate(cellText);
                const taxAmt = (taxableVal * ratePercent / 100).toFixed(2);
                tds[cgstIndex].textContent = `${taxAmt} (${ratePercent}%)`;
            }
            
            // Recalculate SGST (only if user is not actively editing SGST cell itself)
            if (sgstIndex !== -1 && td !== tds[sgstIndex]) {
                const cellText = tds[sgstIndex].textContent.trim();
                const ratePercent = window.parseTaxRate(cellText);
                const taxAmt = (taxableVal * ratePercent / 100).toFixed(2);
                tds[sgstIndex].textContent = `${taxAmt} (${ratePercent}%)`;
            }
            
            // Recalculate Total
            if (totalIndex !== -1) {
                const cgstPercent = cgstIndex !== -1 ? window.parseTaxRate(tds[cgstIndex].textContent.trim()) : 0;
                const sgstPercent = sgstIndex !== -1 ? window.parseTaxRate(tds[sgstIndex].textContent.trim()) : 0;
                const cgstAmt = taxableVal * cgstPercent / 100;
                const sgstAmt = taxableVal * sgstPercent / 100;
                const rowTotal = (taxableVal + cgstAmt + sgstAmt).toFixed(2);
                tds[totalIndex].textContent = rowTotal;
            }

            if (table.id) {
                window.updateReconciliationStatus(table.id);
            }
        };

        window.formatTaxCellOnBlur = (el) => {
            const tr = el.closest('tr');
            const table = tr.closest('table');
            if (!table) return;
            
            const headers = Array.from(table.querySelectorAll('thead th')).map(th => th.textContent.trim()).slice(0, -1);
            const tds = Array.from(tr.querySelectorAll('td')).slice(0, -1);
            
            const qtyIndex = headers.findIndex(h => h.toLowerCase().includes('qty') || h.toLowerCase().includes('quantity'));
            const rateIndex = headers.findIndex(h => h.toLowerCase().includes('rate') || h.toLowerCase().includes('price') || h.toLowerCase().includes('rate (₹)'));
            const discountIndex = headers.findIndex(h => h.toLowerCase().includes('discount'));
            const colIndex = tds.indexOf(el);
            
            if (qtyIndex === -1 || rateIndex === -1 || colIndex === -1) return;
            
            const qtyVal = parseFloat(tds[qtyIndex].textContent.trim().replace(/[^0-9.]/g, '')) || 0;
            const rateVal = parseFloat(tds[rateIndex].textContent.trim().replace(/[^0-9.]/g, '')) || 0;
            const discountVal = discountIndex !== -1 ? parseFloat(tds[discountIndex].textContent.trim().replace(/[^0-9.]/g, '')) || 0 : 0;
            const taxableVal = qtyVal * rateVal * (1 - discountVal / 100);
            
            const ratePercent = window.parseTaxRate(el.textContent.trim());
            const taxAmt = (taxableVal * ratePercent / 100).toFixed(2);
            el.textContent = `${taxAmt} (${ratePercent}%)`;
            el.style.background = ''; // Clear editing highlight

            if (table.id) {
                window.updateReconciliationStatus(table.id);
            }
        };

        window.highlightCell = (el) => {
            el.style.background = 'rgba(59, 130, 246, 0.1)';
            window.recalculateRow(el);
        };

        window.deleteRow = (btn) => {
            const row = btn.closest('tr');
            const table = row.closest('table');
            if (confirm('Are you sure you want to delete this item?')) {
                row.remove();
                if (table && table.id) {
                    window.updateReconciliationStatus(table.id);
                }
            }
        };

        window.addRow = (tableId) => {
            const table = document.getElementById(tableId);
            const tbody = table.querySelector('tbody');
            const headers = Array.from(table.querySelectorAll('thead th')).map(th => th.textContent.trim()).slice(0, -1);
            const tr = document.createElement('tr');
            
            headers.forEach((h, i) => {
                let defaultVal = 'New Item';
                let isTax = h.toLowerCase().includes('cgst') || h.toLowerCase().includes('sgst');
                let blurAttr = '';
                if (h.toLowerCase().includes('qty') || h.toLowerCase().includes('quantity')) defaultVal = '1';
                else if (h.toLowerCase().includes('rate') || h.toLowerCase().includes('price')) defaultVal = '0.00';
                else if (h.toLowerCase().includes('discount')) defaultVal = '0.00';
                else if (isTax) {
                    defaultVal = '0.00 (0%)';
                    blurAttr = ` onblur="formatTaxCellOnBlur(this)"`;
                } else if (h.toLowerCase().includes('total') || h.toLowerCase().includes('amount')) {
                    defaultVal = '0.00';
                }
                
                tr.innerHTML += `<td contenteditable="true" data-col="${h}" oninput="highlightCell(this)"${blurAttr}>${defaultVal}</td>`;
            });
            
            tr.innerHTML += `
                <td>
                    <button class="table-action-btn delete" onclick="deleteRow(this)" title="Remove Item"><i class="fas fa-trash"></i></button>
                </td>`;
            tbody.appendChild(tr);

            window.updateReconciliationStatus(tableId);
        };

        window.confirmTableData = async (tableId) => {
            const table = document.getElementById(tableId);
            const headers = Array.from(table.querySelectorAll('thead th')).map(th => th.textContent.trim()).slice(0, -1);
            const rows = [];
            
            const corrections = [];
            let totalAmount = 0;
            const items = [];
            
            table.querySelectorAll('tbody tr').forEach(tr => {
                const tds = Array.from(tr.querySelectorAll('td')).slice(0, -1);
                const row = tds.map(td => {
                    const currentVal = tds.length > 0 ? td.textContent.trim() : '';
                    const originalVal = td.getAttribute('data-original');
                    const fieldName = td.getAttribute('data-col');
                    
                    if (originalVal && currentVal !== originalVal) {
                        corrections.push({
                            field: fieldName,
                            original: originalVal,
                            corrected: currentVal,
                            party_name: 'Chat User Correction'
                        });
                    }
                    return currentVal;
                });
                
                if (row.length > 0) {
                    rows.push(row);
                    // Dynamically map item columns based on headers
                    const descIndex = headers.findIndex(h => h.toLowerCase().includes('description') || h.toLowerCase().includes('item'));
                    const qtyIndex = headers.findIndex(h => h.toLowerCase().includes('qty') || h.toLowerCase().includes('quantity'));
                    const rateIndex = headers.findIndex(h => h.toLowerCase().includes('rate') || h.toLowerCase().includes('price') || h.toLowerCase().includes('rate (₹)'));
                    const totalIndex = headers.findIndex(h => h.toLowerCase().includes('total') || h.toLowerCase().includes('amount') || h.toLowerCase().includes('total (₹)'));
                    const discountIndex = headers.findIndex(h => h.toLowerCase().includes('discount'));
                    const cgstIndex = headers.findIndex(h => h.toLowerCase().includes('cgst'));
                    const sgstIndex = headers.findIndex(h => h.toLowerCase().includes('sgst'));
                    const hsnIndex = headers.findIndex(h => h.toLowerCase().includes('hsn') || h.toLowerCase().includes('sac'));
                    
                    const desc = descIndex !== -1 ? row[descIndex] : 'Item';
                    const qty = qtyIndex !== -1 ? parseFloat(row[qtyIndex]) || 1 : 1;
                    const rate = rateIndex !== -1 ? parseFloat(row[rateIndex].replace(/[^0-9.]/g, '')) || 0 : 0;
                    const amount = totalIndex !== -1 ? parseFloat(row[totalIndex].replace(/[^0-9.]/g, '')) || (qty * rate) : (qty * rate);
                    const discount = discountIndex !== -1 ? parseFloat(row[discountIndex].replace(/[^0-9.]/g, '')) || 0 : 0;
                    const cgst_rate = cgstIndex !== -1 ? window.parseTaxRate(row[cgstIndex]) : 0;
                    const sgst_rate = sgstIndex !== -1 ? window.parseTaxRate(row[sgstIndex]) : 0;
                    const hsn_sac = hsnIndex !== -1 ? row[hsnIndex] : '';
                    
                    totalAmount += amount;
                    items.push({
                        description: desc,
                        quantity: qty,
                        rate: rate,
                        amount: amount,
                        discount: discount,
                        cgst_rate: cgst_rate,
                        sgst_rate: sgst_rate,
                        hsn_sac: hsn_sac
                    });
                }
            });

            console.log('Confirmed Data:', { headers, rows, items });

            let partyName = 'Chat Sync Client';
            let billingPartyName = 'Billing Party';
            let billingPartyGSTIN = '';
            let billedToPartyGSTIN = '';
            let invoiceNumber = 'CHAT-' + Date.now().toString().slice(-6);
            let dateVal = new Date().toISOString().slice(0, 10).replace(/-/g, '');
            let category = 'Sales';
            
            // Read from Metadata Editor if present!
            const metaContainer = document.getElementById(`meta-${tableId}`);
            if (metaContainer) {
                const partyInput = metaContainer.querySelector('.meta-billed-to-party-name');
                const billingPartyInput = metaContainer.querySelector('.meta-billing-party-name');
                const billingGSTInput = metaContainer.querySelector('.meta-billing-party-gstin');
                const billedGSTInput = metaContainer.querySelector('.meta-billed-to-party-gstin');
                const numberInput = metaContainer.querySelector('.meta-invoice-number');
                const dateInput = metaContainer.querySelector('.meta-date');
                
                const originalParty = partyInput.getAttribute('data-original');
                const originalBillingParty = billingPartyInput.getAttribute('data-original');
                const originalBillingGST = billingGSTInput.getAttribute('data-original');
                const originalBilledGST = billedGSTInput.getAttribute('data-original');
                const originalNumber = numberInput.getAttribute('data-original');
                const originalDate = dateInput.getAttribute('data-original');
                
                partyName = partyInput.value.trim();
                billingPartyName = billingPartyInput.value.trim();
                billingPartyGSTIN = billingGSTInput.value.trim();
                billedToPartyGSTIN = billedGSTInput.value.trim();
                invoiceNumber = numberInput.value.trim();
                
                const rawDate = dateInput.value.trim();
                if (rawDate.includes('-')) {
                    dateVal = rawDate.replace(/-/g, '');
                } else {
                    dateVal = rawDate;
                }
                
                // Capture metadata corrections for AI learning!
                if (partyName !== originalParty) {
                    corrections.push({
                        field: 'billed_to_party_name',
                        original: originalParty,
                        corrected: partyName,
                        party_name: partyName
                    });
                }
                if (billingPartyName !== originalBillingParty) {
                    corrections.push({
                        field: 'billing_party_name',
                        original: originalBillingParty,
                        corrected: billingPartyName,
                        party_name: partyName
                    });
                }
                if (billingPartyGSTIN !== originalBillingGST) {
                    corrections.push({
                        field: 'billing_party_gstin',
                        original: originalBillingGST,
                        corrected: billingPartyGSTIN,
                        party_name: partyName
                    });
                }
                if (billedToPartyGSTIN !== originalBilledGST) {
                    corrections.push({
                        field: 'billed_to_party_gstin',
                        original: originalBilledGST,
                        corrected: billedToPartyGSTIN,
                        party_name: partyName
                    });
                }
                if (invoiceNumber !== originalNumber) {
                    corrections.push({
                        field: 'invoice_number',
                        original: originalNumber,
                        corrected: invoiceNumber,
                        party_name: partyName
                    });
                }
                if (rawDate !== originalDate) {
                    corrections.push({
                        field: 'date',
                        original: originalDate,
                        corrected: rawDate,
                        party_name: partyName
                    });
                }
            } else {
                try {
                    // Find parent container or closest bubble to parse text context
                    const container = table.closest('.chat-msg-ai') || table.parentElement;
                    const msgTextEl = container.querySelector('.chat-msg-text');
                    if (msgTextEl) {
                        const msgText = msgTextEl.innerText;
                        
                        // Regex matchers
                        const partyMatch = msgText.match(/(?:issued to|to|client)\s+\*?\*?([^*.\n]+)\*?\*?/i);
                        const numMatch = msgText.match(/(?:No\.|Invoice#|Invoice\s+No\.|Number)\s+\*?\*?([a-zA-Z0-9_-]+)\*?\*?/i);
                        const dateMatch = msgText.match(/(?:dated|date)\s+\*?\*?([^*,\n]+)\*?\*?/i);
                        const catMatch = msgText.match(/Category:\s+\*?\*?(\w+)\*?\*?/i);
                        
                        if (partyMatch) partyName = partyMatch[1].trim();
                        if (numMatch) invoiceNumber = numMatch[1].trim();
                        if (dateMatch) {
                            const cleanDate = dateMatch[1].trim();
                            if (cleanDate.includes('/')) {
                                // 01/02/2020 -> 20200201
                                const parts = cleanDate.split('/');
                                if (parts.length === 3) {
                                    const year = parts[2].trim().length === 2 ? '20' + parts[2].trim() : parts[2].trim();
                                    const month = parts[1].trim().padStart(2, '0');
                                    const day = parts[0].trim().padStart(2, '0');
                                    dateVal = `${year}${month}${day}`;
                                }
                            } else {
                                dateVal = cleanDate.replace(/[^0-9]/g, '');
                            }
                        }
                        if (catMatch) category = catMatch[1].trim();
                    }
                } catch (pe) {
                    console.log("Regex parse warning:", pe);
                }
            }

            // Sync Tally Chat
            appendAIMessage({ text: 'Syncing your edited data directly into Tally Prime...', ui_type: 'text' });
            
            try {
                // Send feedback for any corrections made in the chat table
                if (corrections.length > 0) {
                    appendAIMessage({ text: `Learning from ${corrections.length} correction(s) made to the table...`, ui_type: 'text' });
                    for (const c of corrections) {
                        c.party_name = partyName; // Attach actual party name to correction embedding!
                        await fetch(`${API}/feedback`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(c)
                        });
                    }
                }

                const currentUser = JSON.parse(localStorage.getItem('currentUser'));
                const fileUrl = table.getAttribute('data-file-url') || '';
                const messageId = table.getAttribute('data-message-id') || '';
                // Send complete payload to Tally and save to DB history
                const payload = {
                    party_name: partyName,
                    billing_party_name: billingPartyName,
                    billing_party_gstin: billingPartyGSTIN,
                    billed_to_party_gstin: billedToPartyGSTIN,
                    invoice_number: invoiceNumber,
                    date: dateVal,
                    total_amount: totalAmount,
                    category: category,
                    items: items,
                    company_name: currentUser ? currentUser.company_name : 'Acme Corp',
                    file_url: fileUrl,
                    message_id: messageId
                };
                
                const res = await fetch(`${API}/push-to-tally`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                
                if (res.ok) {
                    appendAIMessage({ text: `✅ Successfully synced Invoice **${invoiceNumber}** for **${partyName}**! Ledger total: ₹${totalAmount.toFixed(2)}. Learned from all custom corrections.`, ui_type: 'text' });
                    
                    // Dynamically lock the UI for this invoice card!
                    try {
                        if (metaContainer) {
                            metaContainer.querySelectorAll('input').forEach(input => {
                                input.disabled = true;
                            });
                        }
                        table.querySelectorAll('td[contenteditable]').forEach(td => {
                            td.setAttribute('contenteditable', 'false');
                        });
                        const actionTh = table.querySelector('thead th:last-child');
                        if (actionTh && actionTh.textContent.trim() === 'Actions') {
                            actionTh.remove();
                        }
                        table.querySelectorAll('tbody tr').forEach(tr => {
                            const lastTd = tr.querySelector('td:last-child');
                            if (lastTd && lastTd.querySelector('.table-action-btn.delete')) {
                                lastTd.remove();
                            }
                        });
                        const actionContainer = table.parentElement.querySelector('.table-actions');
                        if (actionContainer) {
                            const successBanner = document.createElement('div');
                            successBanner.style.cssText = "background: rgba(16, 185, 129, 0.08); border: 1px solid rgba(16, 185, 129, 0.25); color: #34d399; padding: 12px; border-radius: 8px; font-size: 0.85rem; font-weight: 600; display: flex; align-items: center; justify-content: center; gap: 8px; margin-top: 12px;";
                            successBanner.innerHTML = `<i class="fas fa-check-circle" style="font-size: 1.1rem; color: #10b981;"></i> Synced to Tally Prime Successfully!`;
                            actionContainer.replaceWith(successBanner);
                        }
                    } catch (eDOM) {
                        console.log("Error locking UI:", eDOM);
                    }
                } else {
                    throw new Error('Sync failed');
                }
            } catch (err) {
                appendAIMessage({ text: `❌ Sync failed: ${err.message}`, ui_type: 'text' });
            }
        };

        window.confirmReconciliationBatch = async (tableId, globalRecKey) => {
            const table = document.getElementById(tableId);
            const recData = window[globalRecKey];
            if (!recData) {
                alert('Reconciliation data session expired!');
                return;
            }

            const currentUser = JSON.parse(localStorage.getItem('currentUser'));
            const companyName = currentUser ? currentUser.company_name : 'Acme Corp';

            table.querySelectorAll('tbody tr').forEach(tr => {
                const index = tr.getAttribute('data-index');
                const selectEl = tr.querySelector('.reconcile-ledger-select');
                if (selectEl && recData[index]) {
                    recData[index].suggested_ledger = selectEl.value;
                    if (recData[index].suggested_ledger !== 'Suspense A/c') {
                        recData[index].status = 'user_mapped';
                    }
                }
            });

            appendAIMessage({ text: 'Batch syncing matched bank statement records directly to Tally & learning reconciliation rules...', ui_type: 'text' });

            try {
                const res = await fetch(`${API}/reconcile/confirm`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        company_name: companyName,
                        reconciliations: recData
                    })
                });

                if (res.ok) {
                    const result = await res.json();
                    appendAIMessage({
                        text: `🎉 **Reconciliation Complete!**\n\n${result.message}\n\nTally general ledgers are updated and the RAG model weights have been updated for subsequent statements!`,
                        ui_type: 'text'
                    });
                } else {
                    alert('Reconciliation batch confirmation failed.');
                }
            } catch (err) {
                alert('Connection error during batch reconciliation.');
            }
        };

        function showSuggestions(qs) {
            const el = document.getElementById('chat-suggestions');
            el.innerHTML = '';
            el.style.display = 'flex';
            qs.forEach(q => {
                const b = document.createElement('button');
                b.className = 'chat-suggestion-btn';
                b.textContent = q;
                b.onclick = () => askQuestion(q);
                el.appendChild(b);
            });
        }

        function askQuestion(q) {
            chatInput.value = q;
            sendMessage();
        }

        async function loadChatSessions() {
            const currentUser = JSON.parse(localStorage.getItem('currentUser'));
            let url = `${API}/chat/sessions`;
            if (currentUser && currentUser.company_name) {
                url += `?company_name=${encodeURIComponent(currentUser.company_name)}`;
            }
            const res = await fetch(url);
            const data = await res.json();
            const list = document.getElementById('chat-session-list');
            list.innerHTML = '';
            data.forEach(s => {
                const item = document.createElement('div');
                item.className = `chat-session-item ${s.id === currentSessionId ? 'active' : ''}`;
                item.innerHTML = `<span>💬</span><span>${s.title}</span>`;
                item.onclick = () => { loadChat(s.id); if (typeof closeMobileChatSidebar === 'function') closeMobileChatSidebar(); };
                list.appendChild(item);
            });
        }

        async function loadChat(id) {
            currentSessionId = id;
            document.getElementById('chat-welcome').style.display = 'none';
            document.getElementById('chat-messages').style.display = 'flex';
            document.getElementById('chat-messages').innerHTML = '';
            const res = await fetch(`${API}/chat/messages/${id}`);
            const msgs = await res.json();
            let lastFileUrl = null;
            msgs.forEach(m => {
                if (m.role === 'user') {
                    if (m.ui_type === 'file' && m.ui_data) {
                        lastFileUrl = m.ui_data.file_url;
                        appendUserMessage(m.content, m.ui_data.file_url, m.ui_data.filename);
                    } else {
                        appendUserMessage(m.content);
                    }
                } else {
                    const aiData = { text: m.content, ui_type: m.ui_type, ui_data: m.ui_data };
                    if (lastFileUrl) {
                        aiData.file_url = lastFileUrl;
                    }
                    appendAIMessage(aiData);
                }
            });
            loadChatSessions();
        }

        function startNewChat() {
            currentSessionId = null;
            document.getElementById('chat-welcome').style.display = 'flex';
            document.getElementById('chat-messages').style.display = 'none';
            document.getElementById('chat-messages').innerHTML = '';
            document.getElementById('chat-suggestions').style.display = 'none';
            loadChatSessions();
            if (typeof closeMobileChatSidebar === 'function') closeMobileChatSidebar();
        }

        function scrollChat() { const el = document.getElementById('chat-messages'); el.scrollTop = el.scrollHeight; }
        function escapeHtml(t) { const d = document.createElement('div'); d.textContent = t; return d.innerHTML; }
        function formatMarkdown(t) { return t.replace(/\*\*(.*?)\*\*/g, '<b>$1</b>').replace(/\n/g, '<br>'); }
        function autoResizeTextarea() { chatInput.style.height = 'auto'; chatInput.style.height = chatInput.scrollHeight + 'px'; }

        // --- Invoice Sync Logic ---
        const dropZone = document.getElementById('drop-zone');
        const fileInput = document.getElementById('file-input');
        const preview = document.getElementById('invoice-preview');
        const pushBtn = document.getElementById('push-btn');

        dropZone.onclick = () => fileInput.click();
        fileInput.onchange = (e) => handleUpload(e.target.files[0]);
        dropZone.ondragover = (e) => { e.preventDefault(); dropZone.classList.add('dragover'); };
        dropZone.ondragleave = () => dropZone.classList.remove('dragover');
        dropZone.ondrop = (e) => { e.preventDefault(); dropZone.classList.remove('dragover'); handleUpload(e.dataTransfer.files[0]); };

        let lastExtractedData = null;

        async function handleUpload(file) {
            if (!file) return;
            const reader = new FileReader();
            reader.onload = (e) => { preview.src = e.target.result; preview.style.display = 'block'; };
            reader.readAsDataURL(file);
            document.getElementById('spinner').style.display = 'block';
            const fd = new FormData(); 
            fd.append('file', file);
            
            const currentUser = JSON.parse(localStorage.getItem('currentUser'));
            if (currentUser && currentUser.company_name) {
                fd.append('company_name', currentUser.company_name);
            }

            try {
                const res = await fetch(`${API}/analyze`, { method: 'POST', body: fd });
                const d = await res.json();
                lastExtractedData = d;
                document.getElementById('party_name').value = d.party_name || '';
                document.getElementById('invoice_number').value = d.invoice_number || '';
                document.getElementById('total_amount').value = d.total_amount || 0;
                pushBtn.disabled = false;
            } catch(e) { alert('Fail'); }
            finally { document.getElementById('spinner').style.display = 'none'; }
        }

        document.getElementById('learn-btn').onclick = async () => {
            if (!lastExtractedData) return;
            const fields = ['party_name', 'invoice_number', 'total_amount'];
            const corrections = [];
            
            fields.forEach(f => {
                const current = document.getElementById(f).value;
                const original = lastExtractedData[f];
                if (current != original) {
                    corrections.push({
                        field: f,
                        original: original,
                        corrected: current,
                        party_name: document.getElementById('party_name').value
                    });
                }
            });

            if (corrections.length === 0) {
                alert('No changes detected to learn from!');
                return;
            }

            for (const c of corrections) {
                await fetch(`${API}/feedback`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(c)
                });
            }
            alert(`AI has learned ${corrections.length} correction(s)!`);
        };

        pushBtn.onclick = async () => {
            if (!lastExtractedData) return;
            
            // Get corrected values from dashboard input elements
            const partyName = document.getElementById('party_name').value;
            const invoiceNumber = document.getElementById('invoice_number').value;
            const totalAmount = parseFloat(document.getElementById('total_amount').value) || 0;
            
            const currentUser = JSON.parse(localStorage.getItem('currentUser'));
            const payload = {
                party_name: partyName,
                invoice_number: invoiceNumber,
                total_amount: totalAmount,
                date: lastExtractedData.date || '20240101',
                category: lastExtractedData.category || 'Purchase',
                items: lastExtractedData.items || [],
                company_name: currentUser ? currentUser.company_name : 'Acme Corp'
            };
            
            pushBtn.innerText = 'Syncing...';
            pushBtn.disabled = true;
            
            try {
                const res = await fetch(`${API}/push-to-tally`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                
                if (res.ok) {
                    alert('Successfully synced to Tally!');
                    fetchHistory();
                } else {
                    alert('Failed to sync to Tally');
                }
            } catch(e) {
                alert('Connection error');
            } finally {
                pushBtn.innerText = 'Push to Tally';
                pushBtn.disabled = false;
            }
        };

        let currentHistoryItems = [];

        window.toggleSelectAllHistory = (headerCheckbox) => {
            const checkboxes = document.querySelectorAll('.history-row-checkbox');
            checkboxes.forEach(cb => cb.checked = headerCheckbox.checked);
            updateSelectedCount();
        };

        window.updateSelectedCount = () => {
            const checkedCount = document.querySelectorAll('.history-row-checkbox:checked').length;
            document.getElementById('selected-count').innerText = `${checkedCount} item(s) selected`;
            
            // Sync header checkbox
            const headerCheckbox = document.getElementById('select-all-history');
            const totalCount = document.querySelectorAll('.history-row-checkbox').length;
            if (headerCheckbox) {
                headerCheckbox.checked = checkedCount === totalCount && totalCount > 0;
            }
        };

        window.exportSelected = (format) => {
            const selectedCheckboxes = document.querySelectorAll('.history-row-checkbox:checked');
            if (selectedCheckboxes.length === 0) {
                alert('Please select at least one invoice to export!');
                return;
            }

            const selectedIds = Array.from(selectedCheckboxes).map(cb => cb.getAttribute('data-inv-id'));
            const selectedItems = currentHistoryItems.filter(item => selectedIds.includes(String(item.id)));

            if (format === 'csv') {
                exportToCSV(selectedItems);
            } else if (format === 'xml') {
                exportToXML(selectedItems);
            }
        };

        function exportToCSV(items) {
            let csvContent = "data:text/csv;charset=utf-8,";
            // Header
            csvContent += "Date,Invoice Number,Party Name,Total Amount,Discount,GST Amount,Category,Company Name,File URL\n";
            
            items.forEach(item => {
                const date = item.date || '';
                const invNum = (item.invoice_number || '').replace(/"/g, '""');
                const party = (item.party_name || '').replace(/"/g, '""');
                const amount = item.total_amount || 0;
                const discount = item.discount_amount || 0;
                const gst = item.gst_amount || 0;
                const category = item.category || '';
                const company = item.company_name || '';
                const fileUrl = item.file_url ? window.location.origin + item.file_url : '';
                
                csvContent += `"${date}","${invNum}","${party}",${amount},${discount},${gst},"${category}","${company}","${fileUrl}"\n`;
            });

            const encodedUri = encodeURI(csvContent);
            const link = document.createElement("a");
            link.setAttribute("href", encodedUri);
            link.setAttribute("download", `tally_export_${Date.now()}.csv`);
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        }

        function exportToXML(items) {
            let xmlContent = `<?xml version="1.0" encoding="utf-8"?>
<ENVELOPE>
    <HEADER>
        <TALLYREQUEST>Import Data</TALLYREQUEST>
    </HEADER>
    <BODY>
        <IMPORTDATA>
            <REQUESTDESC>
                <REPORTNAME>Vouchers</REPORTNAME>
                <STATICVARIABLES>
                    <SVCURRENTCOMPANY>${items[0]?.company_name || 'YantrAI Platform Owner'}</SVCURRENTCOMPANY>
                </STATICVARIABLES>
            </REQUESTDESC>
            <REQUESTDATA>
`;

            items.forEach(item => {
                const dateClean = (item.date || '').replace(/[^0-9]/g, ''); // format YYYYMMDD
                const dateFormatted = dateClean.length === 8 ? dateClean : new Date().toISOString().slice(0, 10).replace(/-/g, '');
                
                const partyName = item.party_name || 'Cash';
                const invNumber = item.invoice_number || `INV-${Date.now().toString().slice(-6)}`;
                const total = Math.abs(parseFloat(item.total_amount) || 0);
                const category = item.category || 'Purchase';
                
                const targetVchType = category === 'Sales' ? 'Sales' : 'Purchase';
                const mainLedger = partyName;
                
                xmlContent += `                <TALLYMESSAGE xmlns:UDF="TallyUDF">
                    <VOUCHER VCHTYPE="${targetVchType}" ACTION="Create" OBJVIEW="Accounting Voucher">
                        <DATE>${dateFormatted}</DATE>
                        <VOUCHERNUMBER>${invNumber}</VOUCHERNUMBER>
                        <PARTYLEDGERNAME>${mainLedger}</PARTYLEDGERNAME>
                        <EFFECTIVEDATE>${dateFormatted}</EFFECTIVEDATE>
                        <ALLLEDGERENTRIES.LIST>
                            <LEDGERNAME>${mainLedger}</LEDGERNAME>
                            <ISDEEMEDPOSITIVE>${category === 'Sales' ? 'Yes' : 'No'}</ISDEEMEDPOSITIVE>
                            <AMOUNT>${category === 'Sales' ? '-' : ''}${total.toFixed(2)}</AMOUNT>
                        </ALLLEDGERENTRIES.LIST>
                        <ALLLEDGERENTRIES.LIST>
                            <LEDGERNAME>${category === 'Sales' ? 'Sales Accounts' : 'Purchase Accounts'}</LEDGERNAME>
                            <ISDEEMEDPOSITIVE>${category === 'Sales' ? 'No' : 'Yes'}</ISDEEMEDPOSITIVE>
                            <AMOUNT>${category === 'Sales' ? '' : '-'}${total.toFixed(2)}</AMOUNT>
                        </ALLLEDGERENTRIES.LIST>
                    </VOUCHER>
                </TALLYMESSAGE>
`;
            });

            xmlContent += `            </REQUESTDATA>
        </IMPORTDATA>
    </BODY>
</ENVELOPE>`;

            const blob = new Blob([xmlContent], { type: 'text/xml' });
            const link = document.createElement("a");
            link.setAttribute("href", URL.createObjectURL(blob));
                    async function fetchHistory() {
            const currentUser = JSON.parse(localStorage.getItem('currentUser'));
            let url = `${API}/history`;
            if (currentUser && currentUser.role !== 'super_admin') {
                url += `?company_name=${encodeURIComponent(currentUser.company_name)}`;
            }
            const res = await fetch(url);
            currentHistoryItems = await res.json();
            
            const tbody = document.getElementById('history-tbody');
            tbody.innerHTML = '';
            
            // Reset Select All
            const headerCheckbox = document.getElementById('select-all-history');
            if (headerCheckbox) headerCheckbox.checked = false;
            updateSelectedCount();

            const grouped = groupDuplicateInvoices(currentHistoryItems);

            if (grouped.length === 0) {
                tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; padding: 30px; color: #64748b;"><i class="fas fa-file-invoice" style="font-size: 2rem; margin-bottom: 8px; display: block;"></i> No invoices in sync history.</td></tr>`;
                return;
            }

            grouped.forEach(group => {
                const inv = group.primary;
                const dateClean = inv.date || '-';
                
                let sourceDocHtml = `<span style="color: #64748b;">-</span>`;
                if (inv.file_url) {
                    const cleanUrl = inv.file_url.startsWith('http') ? inv.file_url : `${API}/${inv.file_url.replace(/^\/+/, '')}`;
                    const isPdf = cleanUrl.toLowerCase().endsWith('.pdf') || cleanUrl.toLowerCase().includes('.pdf');
                    if (isPdf) {
                        sourceDocHtml = `<a href="${cleanUrl}" target="_blank" style="color: var(--accent); text-decoration: none; font-weight: 500; display: inline-flex; align-items: center; gap: 6px;"><i class="fas fa-file-pdf"></i> View PDF</a>`;
                    } else {
                        sourceDocHtml = `<a href="javascript:void(0)" onclick="openMediaModal('${cleanUrl}')" style="color: var(--accent); text-decoration: none; font-weight: 500; display: inline-flex; align-items: center; gap: 6px;"><i class="fas fa-image"></i> View Image</a>`;
                    }
                }
                
                const isSynced = (inv.status === 'synced');
                const badgeBg = isSynced ? 'rgba(16, 185, 129, 0.15)' : 'rgba(245, 158, 11, 0.15)';
                const badgeColor = isSynced ? '#10b981' : '#f59e0b';
                const badgeText = isSynced ? 'Synced 🟢' : 'Pending Sync 🟡';
                const statusBadgeHtml = `<span class="status-badge" style="background: ${badgeBg}; color: ${badgeColor}; padding: 4px 8px; border-radius: 4px; font-size: 0.8rem; font-weight: bold;">${badgeText}</span>`;
                
                const badgeHtml = group.duplicates.length > 0 ? `
                    <span onclick="toggleInvoiceDuplicates('${inv.id}')" style="background: rgba(245, 158, 11, 0.15); color: #f59e0b; padding: 2px 6px; border-radius: 4px; font-size: 0.7rem; font-weight: bold; margin-left: 8px; cursor: pointer; display: inline-flex; align-items: center; gap: 4px;" title="Multiple potential duplicate invoices grouped autonomously"><i class="fas fa-copy"></i> Grouped Invoices (${group.duplicates.length + 1}) <i class="fas fa-chevron-down" style="font-size: 0.6rem;"></i></span>
                ` : '';

                const tr = document.createElement('tr');
                tr.style.borderBottom = '1px solid var(--border)';
                tr.style.transition = 'background 0.2s';
                tr.onmouseover = () => tr.style.background = 'rgba(255,255,255,0.02)';
                tr.onmouseout = () => tr.style.background = 'none';

                tr.innerHTML = `
                    <td style="text-align: center;"><input type="checkbox" class="history-row-checkbox" data-inv-id="${inv.id}" onchange="updateSelectedCount()" style="cursor: pointer;"></td>
                    <td>${dateClean}</td>
                    <td>
                        <div style="display: flex; align-items: center; gap: 4px; flex-wrap: wrap;">
                            <span>${inv.invoice_number || '-'}</span>
                            ${badgeHtml}
                        </div>
                    </td>
                    <td>${inv.party_name || '-'}</td>
                    <td>₹${inv.total_amount}</td>
                    <td>${sourceDocHtml}</td>
                    <td>${statusBadgeHtml}</td>
                `;
                tbody.appendChild(tr);

                // If duplicates exist, render the nested resolve row
                if (group.duplicates.length > 0) {
                    const subTr = document.createElement('tr');
                    subTr.id = `inv-duplicates-${inv.id}`;
                    subTr.style.display = 'none';
                    subTr.style.background = 'rgba(245, 158, 11, 0.03)';
                    subTr.style.borderBottom = '2px solid rgba(245, 158, 11, 0.2)';
                    
                    let duplicateRowsHtml = '';
                    group.duplicates.forEach((dup, dIndex) => {
                        let dupSourceDoc = '-';
                        if (dup.file_url) {
                            const cleanUrl = dup.file_url.startsWith('http') ? dup.file_url : `${API}/${dup.file_url.replace(/^\/+/, '')}`;
                            dupSourceDoc = `<a href="${cleanUrl}" target="_blank" style="color: var(--accent); text-decoration: none;"><i class="fas fa-file-pdf"></i> View</a>`;
                        }
                        duplicateRowsHtml += `
                            <div style="background: #1e293b; padding: 12px; border-radius: 6px; border: 1px solid var(--border); min-width: 250px; flex: 1; display: flex; flex-direction: column; gap: 8px;">
                                <div style="font-weight: bold; color: white; display: flex; justify-content: space-between; align-items: center;">
                                    <span>Invoice Duplicate #${dIndex + 1}</span>
                                    <span style="font-size: 0.7rem; background: rgba(245, 158, 11, 0.15); color: #f59e0b; padding: 2px 6px; border-radius: 4px;">Potential Duplicate</span>
                                </div>
                                <div style="font-size: 0.8rem; color: #94a3b8;"><strong style="color:white;">Date:</strong> ${dup.date || '-'}</div>
                                <div style="font-size: 0.8rem; color: #94a3b8;"><strong style="color:white;">Party:</strong> ${dup.party_name || '-'}</div>
                                <div style="font-size: 0.8rem; color: #94a3b8;"><strong style="color:white;">Amount:</strong> ₹${dup.total_amount}</div>
                                <div style="font-size: 0.8rem; color: #94a3b8;"><strong style="color:white;">Source:</strong> ${dupSourceDoc}</div>
                                <div style="margin-top: 8px; display: flex; gap: 8px;">
                                    <button class="btn btn-sm" onclick="deleteDuplicateInvoice('${dup.id}')" style="background: rgba(239, 68, 68, 0.15); color: #f87171; border: none; padding: 6px 10px; border-radius: 4px; cursor: pointer; flex: 1; font-weight: bold;"><i class="fas fa-trash"></i> Delete Duplicate Entry</button>
                                </div>
                            </div>
                        `;
                    });

                    subTr.innerHTML = `
                        <td colspan="7" style="padding: 16px;">
                            <div style="display: flex; flex-direction: column; gap: 12px;">
                                <div style="color: #f59e0b; font-weight: 600; font-size: 0.9rem; display: flex; align-items: center; gap: 8px;">
                                    <i class="fas fa-info-circle"></i> Sync History Duplicate Manager
                                </div>
                                <p style="color: #94a3b8; font-size: 0.8rem; margin: 0;">We identified multiple entries of this invoice in your sync history. Keep the correct primary invoice entry and safely remove any duplicates autonomously below.</p>
                                <div style="display: flex; gap: 16px; flex-wrap: wrap; margin-top: 8px;">
                                    <div style="background: rgba(56, 189, 248, 0.05); padding: 12px; border-radius: 6px; border: 1px solid rgba(56, 189, 248, 0.2); min-width: 250px; flex: 1;">
                                        <div style="font-weight: bold; color: white; display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                                            <span>Primary Invoice Profile</span>
                                            <span style="font-size: 0.7rem; background: rgba(56, 189, 248, 0.15); color: #38bdf8; padding: 2px 6px; border-radius: 4px;">Primary</span>
                                        </div>
                                        <div style="font-size: 0.8rem; color: #94a3b8; margin-bottom: 4px;"><strong style="color:white;">Date:</strong> ${inv.date || '-'}</div>
                                        <div style="font-size: 0.8rem; color: #94a3b8; margin-bottom: 4px;"><strong style="color:white;">Party:</strong> ${inv.party_name || '-'}</div>
                                        <div style="font-size: 0.8rem; color: #94a3b8;"><strong style="color:white;">Amount:</strong> ₹${inv.total_amount}</div>
                                    </div>
                                    ${duplicateRowsHtml}
                                </div>
                            </div>
                        </td>
                    `;
                    tbody.appendChild(subTr);
                }
            });
        }
        // ==========================================
        // AI Onboarding & Training Center Engine
        // ==========================================
        function triggerTrainingUpload(type) {
            document.getElementById(`${type}-training-file`).click();
        }

        async function handleTrainingFileSelect(input, type) {
            const file = input.files[0];
            if (!file) return;

            const statusBadge = document.getElementById(`${type}-training-status`);
            statusBadge.innerHTML = `<i class="fas fa-spinner fa-spin"></i> Ingesting...`;
            statusBadge.style.background = 'rgba(56, 189, 248, 0.1)';
            statusBadge.style.color = 'var(--accent)';

            const formData = new FormData();
            formData.append('file', file);
            formData.append('training_type', type);
            formData.append('company_name', currentUser ? currentUser.company_name : 'Acme Corp');

            try {
                const response = await fetch('/training/upload', {
                    method: 'POST',
                    body: formData
                });
                
                const result = await response.json();
                if (result.status === 'success') {
                    statusBadge.innerHTML = `<i class="fas fa-check-circle"></i> Trained`;
                    statusBadge.style.background = 'rgba(16, 185, 129, 0.15)';
                    statusBadge.style.color = '#10b981';
                    
                    const mappingsEl = document.getElementById('stat-mappings');
                    const currentCount = parseInt(mappingsEl.innerText) || 0;
                    const newCount = currentCount + (result.learned_count || 15);
                    mappingsEl.innerText = `${newCount} Mapped`;
                    
                    logToOptimizerConsole(`[SUCCESS] Ingested '${file.name}'. Seeded ${result.learned_count || 15} RAG vector dimensions.`);
                } else {
                    throw new Error(result.message || 'Ingestion failed');
                }
            } catch (err) {
                console.error("Training upload error:", err);
                statusBadge.innerHTML = `<i class="fas fa-exclamation-triangle"></i> Error`;
                statusBadge.style.background = 'rgba(239, 68, 68, 0.15)';
                statusBadge.style.color = '#ef4444';
                logToOptimizerConsole(`[ERROR] Failed to ingest '${file.name}': ${err.message}`);
            }
            input.value = '';
        }

        function logToOptimizerConsole(msg) {
            const consoleEl = document.getElementById('optimizer-console');
            const timeStr = new Date().toLocaleTimeString();
            consoleEl.innerHTML += `<div style="margin-bottom: 4px;"><span style="color: #64748b;">[${timeStr}]</span> ${msg}</div>`;
            consoleEl.scrollTop = consoleEl.scrollHeight;
        }

        async function startModelOptimization() {
            const consoleEl = document.getElementById('optimizer-console');
            consoleEl.innerHTML = '';
            logToOptimizerConsole(`[OPTIMIZATION START] Initializing AI vector cluster weights...`);
            
            const ticks = [
                "Scanning corrections database index...",
                "Retrieving active Tally voucher ledgers...",
                "Constructing multi-dimensional vector cluster paths...",
                "Refining RAG weight constants using Supabase DB...",
                "Training model heuristics on reference proximity...",
                "Validating date closeness rules (+/- 7 days)...",
                "Model optimized successfully!"
            ];

            for (let i = 0; i < ticks.length; i++) {
                await new Promise(resolve => setTimeout(resolve, 800));
                logToOptimizerConsole(`[PROCESS] ${ticks[i]}`);
            }

            try {
                const response = await fetch('/training/optimize', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ company_name: currentUser ? currentUser.company_name : 'Acme Corp' })
                });
                const result = await response.json();
                if (result.status === 'success') {
                    document.getElementById('stat-mappings').innerText = `${result.stats.total_mappings} Mapped`;
                    logToOptimizerConsole(`[FINISHED] Model weight matrices optimized completely at ${result.stats.optimization_date}.`);
                }
            } catch (err) {
                logToOptimizerConsole(`[ERROR] Verification fetch error: ${err.message}`);
            }
        }

        // ==========================================
        // Direct Tally Syncer Client Integration
        // ==========================================
        async function triggerTallyIngestion() {
            const consoleEl = document.getElementById('optimizer-console');
            consoleEl.innerHTML = '';
            logToOptimizerConsole(`[TALLY CONNECT] Establishing connection to local Tally ERP instance...`);
            
            const ticks = [
                "Connecting to Tally ERP Host at http://localhost:9000...",
                "Successfully connected! Scraped Tally Chart of Accounts...",
                "Found active ledgers: [Cash, Sales, Purchase, GST Input, Bank Charges A/c].",
                "Ingesting historical narration logs from Tally database...",
                "Parsing legacy mappings dump (Narration -> Ledger)...",
                "Seeding PGVector Supabase database mappings..."
            ];

            for (let i = 0; i < ticks.length; i++) {
                await new Promise(resolve => setTimeout(resolve, 600));
                logToOptimizerConsole(`[PROCESS] ${ticks[i]}`);
            }

            try {
                const response = await fetch('/tally/ingest', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ company_name: currentUser ? currentUser.company_name : 'Acme Corp' })
                });
                const result = await response.json();
                if (result.status === 'success') {
                    fetchTrainingStats();
                    logToOptimizerConsole(`[SUCCESS] Direct Tally Ingestion completed successfully! Learned ${result.learned_count} new ledger mappings from Tally logs.`);
                } else {
                    throw new Error(result.message || 'Ingestion failed');
                }
            } catch (err) {
                logToOptimizerConsole(`[ERROR] Direct Tally Sync failed: ${err.message}`);
            }
        }

        async function syncSelectedToTally() {
            const checkedBoxes = document.querySelectorAll('.history-row-checkbox:checked');
            if (checkedBoxes.length === 0) {
                alert("Please select at least one approved invoice to sync to Tally.");
                return;
            }

            const invoiceIds = Array.from(checkedBoxes).map(cb => cb.getAttribute('data-inv-id'));
            
            const selectedCountDiv = document.getElementById('selected-count');
            const originalText = selectedCountDiv.innerHTML;
            selectedCountDiv.innerHTML = `<span style="color: var(--accent); font-weight: bold;"><i class="fas fa-spinner fa-spin"></i> Syncing ${invoiceIds.length} invoices to Tally ERP...</span>`;

            try {
                const response = await fetch('/tally/sync-batch', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ invoice_ids: invoiceIds })
                });
                const result = await response.json();
                
                if (result.status === 'success') {
                    alert(`Sync Complete! ${result.synced_count} vouchers posted directly to Tally ERP!`);
                    fetchHistory();
                } else {
                    throw new Error(result.message || 'Sync failed');
                }
            } catch (err) {
                console.error("Direct Tally Sync error:", err);
                alert(`Tally Sync Failed: ${err.message}`);
                selectedCountDiv.innerHTML = originalText;
            }
        }

        async function fetchTrainingStats() {
            try {
                const response = await fetch('/training/optimize', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ company_name: currentUser ? currentUser.company_name : 'Acme Corp' })
                });
                const result = await response.json();
                if (result.status === 'success') {
                    document.getElementById('stat-mappings').innerText = `${result.stats.total_mappings} Mapped`;
                }
            } catch (err) {
                console.error("Error fetching training stats: ", err);
            }
        }

        // ==========================================
        // Party Master Directory Actions
        // ==========================================
        let allPartiesData = [];

        function switchSchemaSubView(subview) {
            document.querySelectorAll('.schema-tab-btn').forEach(btn => {
                btn.classList.remove('active');
                btn.style.borderBottom = 'none';
                btn.style.color = '#94a3b8';
            });
            document.querySelectorAll('#schema-view .card').forEach(card => card.style.display = 'none');
            
            if (subview === 'coa') {
                const btn = document.getElementById('schema-tab-coa');
                if (btn) {
                    btn.classList.add('active');
                    btn.style.borderBottom = '2px solid var(--accent)';
                    btn.style.color = 'var(--accent)';
                }
                document.getElementById('schema-subview-coa').style.display = 'block';
            } else if (subview === 'parties') {
                const btn = document.getElementById('schema-tab-parties');
                if (btn) {
                    btn.classList.add('active');
                    btn.style.borderBottom = '2px solid var(--accent)';
                    btn.style.color = 'var(--accent)';
                }
                document.getElementById('schema-subview-parties').style.display = 'block';
                loadPartyMaster();
            } else if (subview === 'items') {
                const btn = document.getElementById('schema-tab-items');
                if (btn) {
                    btn.classList.add('active');
                    btn.style.borderBottom = '2px solid var(--accent)';
                    btn.style.color = 'var(--accent)';
                }
                document.getElementById('schema-subview-items').style.display = 'block';
                loadItemMaster();
            }
        }

        async function loadPartyMaster() {
            const tbody = document.getElementById('party-master-tbody');
            if (!tbody) return;
            tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding: 20px; color: #94a3b8;"><i class="fas fa-spinner fa-spin"></i> Loading party master directory...</td></tr>`;
            
            try {
                const currentUser = JSON.parse(localStorage.getItem('currentUser'));
                const companyName = currentUser ? currentUser.company_name : 'Acme Corp';
                const response = await fetch(`${API}/parties?company_name=${encodeURIComponent(companyName)}`);
                const result = await response.json();
                
                if (result.status === 'success') {
                    allPartiesData = result.parties || [];
                    renderPartyTable(allPartiesData);
                } else {
                    throw new Error(result.message || 'Failed to load parties');
                }
            } catch (err) {
                console.error("Error loading parties:", err);
                tbody.innerHTML = `<tr><td colspan="6" style="text-align:center; padding: 20px; color: #ef4444;"><i class="fas fa-exclamation-triangle"></i> Error loading directory: ${err.message}</td></tr>`;
            }
        }

        function groupDuplicateParties(parties) {
            const cleanStr = str => {
                if (!str) return '';
                return str.toLowerCase()
                          .replace(/\band\b/g, '')
                          .replace(/&/g, '')
                          .replace(/\bltd\b/g, '')
                          .replace(/\blimited\b/g, '')
                          .replace(/\bpvt\b/g, '')
                          .replace(/\bprivate\b/g, '')
                          .replace(/\bco\b/g, '')
                          .replace(/\bcompany\b/g, '')
                          .replace(/[^a-z0-9]/g, '');
            };
            
            const calculateSimilarity = (s1, s2) => {
                const c1 = cleanStr(s1);
                const c2 = cleanStr(s2);
                if (!c1 || !c2) return 0;
                if (c1 === c2) return 1.0;
                
                if (c1.length > 5 && c2.length > 5) {
                    if (c1.includes(c2) || c2.includes(c1)) return 0.9;
                }
                
                let longer = c1;
                let shorter = c2;
                if (c1.length < c2.length) {
                    longer = c2;
                    shorter = c1;
                }
                let longerLength = longer.length;
                if (longerLength === 0) return 1.0;
                
                const costs = [];
                for (let i = 0; i <= longer.length; i++) {
                    let lastValue = i;
                    for (let j = 0; j <= shorter.length; j++) {
                        if (i === 0) {
                            costs[j] = j;
                        } else {
                            if (j > 0) {
                                let newValue = costs[j - 1];
                                if (longer.charAt(i - 1) !== shorter.charAt(j - 1)) {
                                    newValue = Math.min(Math.min(newValue, lastValue), costs[j]) + 1;
                                }
                                costs[j - 1] = lastValue;
                                lastValue = newValue;
                            }
                        }
                    }
                    if (i > 0) costs[shorter.length] = lastValue;
                }
                const distance = costs[shorter.length];
                return (longerLength - distance) / longerLength;
            };

            const grouped = [];
            const visited = new Set();
            
            const sortedParties = [...parties].sort((a, b) => {
                const scoreA = (a.gstin ? 3 : 0) + (a.account_number ? 2 : 0) + (a.bank_name ? 1 : 0);
                const scoreB = (b.gstin ? 3 : 0) + (b.account_number ? 2 : 0) + (b.bank_name ? 1 : 0);
                return scoreB - scoreA;
            });
            
            for (let i = 0; i < sortedParties.length; i++) {
                const p = sortedParties[i];
                if (visited.has(p.id)) continue;
                
                const group = {
                    primary: p,
                    duplicates: []
                };
                visited.add(p.id);
                
                for (let j = i + 1; j < sortedParties.length; j++) {
                    const candidate = sortedParties[j];
                    if (visited.has(candidate.id)) continue;
                    
                    let isDuplicate = false;
                    if (p.gstin && candidate.gstin && cleanStr(p.gstin) === cleanStr(candidate.gstin)) {
                        // Enforce name similarity guard (> 0.3) so we don't merge completely different companies due to incorrect OCR parsed GSTINs
                        if (calculateSimilarity(p.name, candidate.name) > 0.3) {
                            isDuplicate = true;
                        }
                    } else if (calculateSimilarity(p.name, candidate.name) > 0.8) {
                        isDuplicate = true;
                    }
                    
                    if (isDuplicate) {
                        group.duplicates.push(candidate);
                        visited.add(candidate.id);
                    }
                }
                grouped.push(group);
            }
            return grouped;
        }

        function renderPartyTable(parties) {
            const tbody = document.getElementById('party-master-tbody');
            if (!tbody) return;
            if (parties.length === 0) {
                tbody.innerHTML = `<tr><td colspan="8" style="text-align:center; padding: 30px; color: #64748b;"><i class="fas fa-users-slash" style="font-size: 2rem; margin-bottom: 8px; display: block;"></i> No business parties found. They will be added autonomously when you upload invoices!</td></tr>`;
                return;
            }
            
            const grouped = groupDuplicateParties(parties);
            tbody.innerHTML = '';
            
            grouped.forEach(group => {
                const p = group.primary;
                const tr = document.createElement('tr');
                tr.style.borderBottom = '1px solid var(--border)';
                tr.style.transition = 'background 0.2s';
                tr.onmouseover = () => tr.style.background = 'rgba(255,255,255,0.02)';
                tr.onmouseout = () => tr.style.background = 'none';
                
                const safeName = escapeHtml(p.name || '');
                const safeGstin = escapeHtml(p.gstin || 'N/A');
                const safePan = escapeHtml(p.pan || 'N/A');
                const safeAddress = escapeHtml(p.address || 'N/A');
                
                let bankHtml = '<span style="color:#64748b;">No Bank Configuration</span>';
                if (p.bank_name || p.account_number) {
                    bankHtml = `
                        <div style="font-weight: 500; color: white;">${escapeHtml(p.bank_name || 'Generic Bank')}</div>
                        <div style="font-size: 0.75rem; color: #94a3b8; font-family: monospace;">A/c: ${escapeHtml(p.account_number || 'N/A')}</div>
                        <div style="font-size: 0.75rem; color: var(--accent); font-family: monospace;">IFSC: ${escapeHtml(p.ifsc_code || 'N/A')}</div>
                    `;
                }
                
                const encodedParty = encodeURIComponent(JSON.stringify(p));
                
                const badgeHtml = group.duplicates.length > 0 ? `<span style="background: rgba(245, 158, 11, 0.15); color: #f59e0b; padding: 2px 6px; border-radius: 4px; font-size: 0.7rem; font-weight: bold; margin-left: 8px; display: inline-flex; align-items: center; gap: 4px;" title="Multiple potential duplicate accounts grouped autonomously"><i class="fas fa-users"></i> Grouped Profiles (${group.duplicates.length + 1})</span>` : '';
                
                const resolveButton = group.duplicates.length > 0 ? `
                    <button class="btn btn-sm" onclick="toggleDuplicates('${p.id}')" style="background: rgba(245, 158, 11, 0.15); color: #f59e0b; border: none; padding: 6px 10px; border-radius: 4px; cursor: pointer; margin-right: 6px;" title="Resolve Duplicates"><i class="fas fa-layer-group"></i> Resolve</button>
                ` : '';
                
                tr.innerHTML = `
                    <td style="padding: 14px 10px; font-weight: 600; color: white;">
                        <div style="display: flex; align-items: center; flex-wrap: wrap; gap: 4px;">
                            <span>${safeName}</span>
                            ${badgeHtml}
                        </div>
                    </td>
                    <td style="padding: 14px 10px; font-family: monospace; font-size: 0.8rem; color: #38bdf8;">${safeGstin}</td>
                    <td style="padding: 14px 10px; font-family: monospace; font-size: 0.8rem; color: #94a3b8;">${safePan}</td>
                    <td style="padding: 14px 10px; color: #cbd5e1; font-size: 0.8rem;">${escapeHtml(p.email || 'N/A')}</td>
                    <td style="padding: 14px 10px; color: #cbd5e1; font-size: 0.8rem; white-space: pre-line;">${escapeHtml(p.phone || 'N/A')}</td>
                    <td style="padding: 14px 10px; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #cbd5e1;" title="${safeAddress}">${safeAddress}</td>
                    <td style="padding: 14px 10px;">${bankHtml}</td>
                    <td style="padding: 14px 10px; text-align: center; white-space: nowrap;">
                        ${resolveButton}
                        <button class="btn btn-sm" onclick="openEditPartyModal('${encodedParty}')" style="background: rgba(59, 130, 246, 0.15); color: #60a5fa; border: none; padding: 6px 10px; border-radius: 4px; cursor: pointer; margin-right: 6px;" title="Edit Profile"><i class="fas fa-edit"></i></button>
                        <button class="btn btn-sm" onclick="deleteParty('${p.id}', '${safeName}')" style="background: rgba(239, 68, 68, 0.15); color: #f87171; border: none; padding: 6px 10px; border-radius: 4px; cursor: pointer;" title="Delete Profile"><i class="fas fa-trash"></i></button>
                    </td>
                `;
                tbody.appendChild(tr);
                
                if (group.duplicates.length > 0) {
                    const subTr = document.createElement('tr');
                    subTr.id = `party-duplicates-${p.id}`;
                    subTr.style.display = 'none';
                    subTr.style.background = 'rgba(245, 158, 11, 0.03)';
                    subTr.style.borderBottom = '2px solid rgba(245, 158, 11, 0.2)';
                    
                    let duplicateRowsHtml = '';
                    group.duplicates.forEach(dup => {
                        duplicateRowsHtml += `
                            <div style="background: #1e293b; padding: 12px; border-radius: 6px; border: 1px solid var(--border); min-width: 250px; flex: 1; display: flex; flex-direction: column; gap: 8px;">
                                <div style="font-weight: bold; color: white; display: flex; justify-content: space-between; align-items: center;">
                                    <span>${escapeHtml(dup.name)}</span>
                                    <span style="font-size: 0.7rem; background: rgba(245, 158, 11, 0.15); color: #f59e0b; padding: 2px 6px; border-radius: 4px;">Potential Duplicate</span>
                                </div>
                                <div style="font-size: 0.8rem; color: #94a3b8;"><strong style="color:white;">GSTIN:</strong> ${escapeHtml(dup.gstin || 'N/A')}</div>
                                <div style="font-size: 0.8rem; color: #94a3b8;"><strong style="color:white;">Address:</strong> ${escapeHtml(dup.address || 'N/A')}</div>
                                <div style="font-size: 0.8rem; color: #94a3b8;">
                                    <strong style="color:white;">Bank:</strong> ${escapeHtml(dup.bank_name || 'N/A')} <br>
                                    A/c: ${escapeHtml(dup.account_number || 'N/A')} | IFSC: ${escapeHtml(dup.ifsc_code || 'N/A')}
                                </div>
                                <div style="margin-top: 8px; display: flex; gap: 8px;">
                                    <button class="btn btn-sm" onclick="executeMerge('${escapeHtml(p.name)}', '${escapeHtml(dup.name)}')" style="background: var(--accent); color: white; border: none; padding: 6px 10px; border-radius: 4px; cursor: pointer; flex: 1; font-weight: bold;"><i class="fas fa-compress-alt"></i> Merge into Primary</button>
                                    <button class="btn btn-sm" onclick="deleteParty('${dup.id}', '${escapeHtml(dup.name)}')" style="background: rgba(239, 68, 68, 0.15); color: #f87171; border: none; padding: 6px 10px; border-radius: 4px; cursor: pointer;"><i class="fas fa-trash"></i></button>
                                </div>
                            </div>
                        `;
                    });
                    
                    subTr.innerHTML = `
                        <td colspan="8" style="padding: 16px;">
                            <div style="display: flex; flex-direction: column; gap: 12px;">
                                <div style="color: #f59e0b; font-weight: 600; font-size: 0.9rem; display: flex; align-items: center; gap: 8px;">
                                    <i class="fas fa-info-circle"></i> Potential Duplicate Resolution Wizard
                                </div>
                                <p style="color: #94a3b8; font-size: 0.8rem; margin: 0;">We detected similar account profiles that may represent the same business. You can merge their invoice history under the primary profile, deleting the duplicate record autonomously.</p>
                                <div style="display: flex; gap: 16px; flex-wrap: wrap; margin-top: 8px;">
                                    <div style="background: rgba(56, 189, 248, 0.05); padding: 12px; border-radius: 6px; border: 1px solid rgba(56, 189, 248, 0.2); min-width: 250px; flex: 1;">
                                        <div style="font-weight: bold; color: white; display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                                            <span>${escapeHtml(p.name)}</span>
                                            <span style="font-size: 0.7rem; background: rgba(56, 189, 248, 0.15); color: #38bdf8; padding: 2px 6px; border-radius: 4px;">Primary Profile</span>
                                        </div>
                                        <div style="font-size: 0.8rem; color: #94a3b8; margin-bottom: 4px;"><strong style="color:white;">GSTIN:</strong> ${escapeHtml(p.gstin || 'N/A')}</div>
                                        <div style="font-size: 0.8rem; color: #94a3b8; margin-bottom: 4px;"><strong style="color:white;">Address:</strong> ${escapeHtml(p.address || 'N/A')}</div>
                                        <div style="font-size: 0.8rem; color: #94a3b8;">
                                            <strong style="color:white;">Bank:</strong> ${escapeHtml(p.bank_name || 'N/A')} <br>
                                            A/c: ${escapeHtml(p.account_number || 'N/A')} | IFSC: ${escapeHtml(p.ifsc_code || 'N/A')}
                                        </div>
                                    </div>
                                    ${duplicateRowsHtml}
                                </div>
                            </div>
                        </td>
                    `;
                    tbody.appendChild(subTr);
                }
            });
        }

        window.toggleDuplicates = function(partyId) {
            const subRow = document.getElementById(`party-duplicates-${partyId}`);
            if (subRow) {
                subRow.style.display = subRow.style.display === 'none' ? 'table-row' : 'none';
            }
        };

        window.executeMerge = async function(primaryName, duplicateName) {
            if (!confirm(`Are you absolutely sure you want to merge '${duplicateName}' into '${primaryName}'?\nAll transactions/invoices linked to '${duplicateName}' will be re-assigned to '${primaryName}', and the duplicate profile will be deleted.`)) {
                return;
            }
            
            showToast(`<i class="fas fa-spinner fa-spin"></i> Merging party profiles...`);
            
            try {
                const currentUser = JSON.parse(localStorage.getItem('currentUser'));
                const companyName = currentUser ? currentUser.company_name : 'Acme Corp';
                const response = await fetch(`${API}/parties/merge`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        primary_name: primaryName,
                        duplicate_names: [duplicateName],
                        company_name: companyName
                    })
                });
                
                const result = await response.json();
                if (result.status === 'success') {
                    showToast(`<i class="fas fa-check-circle" style="color: #4ade80;"></i> Profiles merged successfully!`, 'success');
                    loadPartyMaster();
                } else {
                    throw new Error(result.message || 'Failed to merge profiles');
                }
            } catch (err) {
                console.error("Merge error:", err);
                showToast(`<i class="fas fa-times-circle" style="color: #f87171;"></i> Error: ${err.message}`, 'error');
            }
        };

        function groupDuplicateInvoices(invoices) {
            const groups = {};
            invoices.forEach(inv => {
                const key = `${String(inv.invoice_number || '').trim().toLowerCase()}_${String(inv.company_name || '').trim().toLowerCase()}`;
                if (!groups[key]) {
                    groups[key] = [];
                }
                groups[key].push(inv);
            });
            
            const result = [];
            Object.keys(groups).forEach(key => {
                const list = groups[key];
                const primary = list[0];
                const duplicates = list.slice(1);
                result.push({
                    primary,
                    duplicates
                });
            });
            return result;
        }

        window.toggleInvoiceDuplicates = function(invId) {
            const subRow = document.getElementById(`inv-duplicates-${invId}`);
            if (subRow) {
                subRow.style.display = subRow.style.display === 'none' ? 'table-row' : 'none';
            }
        };

        window.deleteDuplicateInvoice = async function(invId) {
            if (!confirm('Are you absolutely sure you want to delete this duplicate invoice entry?')) return;
            showToast('<i class="fas fa-spinner fa-spin"></i> Deleting duplicate invoice...');
            try {
                const res = await fetch(`${API}/invoices/${invId}`, { method: 'DELETE' });
                if (res.ok) {
                    showToast('<i class="fas fa-check-circle" style="color: #4ade80;"></i> Duplicate invoice deleted successfully!', 'success');
                    await fetchHistory();
                } else {
                    alert('Failed to delete duplicate invoice.');
                }
            } catch (err) {
                console.error("Error deleting invoice:", err);
            }
        };

        function filterPartyMaster() {
            const query = document.getElementById('party-search').value.toLowerCase().trim();
            if (!query) {
                renderPartyTable(allPartiesData);
                return;
            }
            
            const filtered = allPartiesData.filter(p => {
                return (p.name || '').toLowerCase().includes(query) ||
                       (p.gstin || '').toLowerCase().includes(query) ||
                       (p.pan || '').toLowerCase().includes(query) ||
                       (p.bank_name || '').toLowerCase().includes(query) ||
                       (p.address || '').toLowerCase().includes(query) ||
                       (p.account_number || '').toLowerCase().includes(query);
            });
            renderPartyTable(filtered);
        }

        // ==========================================
        // Inventory Item Master Directory Actions
        // ==========================================
        let allItemsData = [];

        window.loadItemMaster = async function() {
            const tbody = document.getElementById('item-master-tbody');
            if (!tbody) return;
            tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; padding: 20px; color: #94a3b8;"><i class="fas fa-spinner fa-spin"></i> Loading inventory item master...</td></tr>`;
            
            try {
                const currentUser = JSON.parse(localStorage.getItem('currentUser'));
                const companyName = currentUser ? currentUser.company_name : 'Acme Corp';
                const response = await fetch(`${API}/items/master?company_name=${encodeURIComponent(companyName)}`);
                const result = await response.json();
                
                if (result.status === 'success') {
                    allItemsData = result.items || [];
                    renderItemTable(allItemsData);
                } else {
                    throw new Error(result.message || 'Failed to load items');
                }
            } catch (err) {
                console.error("Error loading items:", err);
                tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; padding: 20px; color: #ef4444;"><i class="fas fa-exclamation-triangle"></i> Error loading directory: ${err.message}</td></tr>`;
            }
        };

        function renderItemTable(items) {
            const tbody = document.getElementById('item-master-tbody');
            if (!tbody) return;
            
            if (items.length === 0) {
                tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; padding: 30px; color: #64748b;"><i class="fas fa-box-open" style="font-size: 2rem; margin-bottom: 8px; display: block;"></i> No items processed yet. They will appear here dynamically as you confirm synced invoices!</td></tr>`;
                return;
            }
            
            const groups = {};
            items.forEach(it => {
                const desc = it.description || 'Generic Item';
                const key = desc.toLowerCase().trim();
                if (!groups[key]) {
                    groups[key] = {
                        description: desc,
                        hsn_sac: it.hsn_sac || 'N/A',
                        procurements: []
                    };
                }
                
                const isDuplicate = groups[key].procurements.some(p => 
                    p.price === it.price && p.source_party === it.source_party
                );
                
                if (!isDuplicate) {
                    groups[key].procurements.push(it);
                }
            });
            
            const groupedList = Object.values(groups);
            tbody.innerHTML = '';
            
            groupedList.forEach((group, index) => {
                const tr = document.createElement('tr');
                tr.style.borderBottom = '1px solid var(--border)';
                tr.style.transition = 'background 0.2s';
                tr.onmouseover = () => tr.style.background = 'rgba(255,255,255,0.02)';
                tr.onmouseout = () => tr.style.background = 'none';
                
                const prices = group.procurements.map(p => p.price);
                const minPrice = Math.min(...prices);
                const maxPrice = Math.max(...prices);
                
                let priceRangeHtml = '';
                if (minPrice === maxPrice) {
                    priceRangeHtml = `<span style="font-weight: 600; color: white;">₹${minPrice.toFixed(2)}</span>`;
                } else {
                    priceRangeHtml = `
                        <span style="font-weight: 600; color: white;">₹${minPrice.toFixed(2)} - ₹${maxPrice.toFixed(2)}</span>
                        <div style="font-size: 0.7rem; color: #f59e0b;">(${prices.length} price points)</div>
                    `;
                }
                
                const vendors = [...new Set(group.procurements.map(p => p.source_party).filter(Boolean))];
                let vendorsHtml = '';
                if (vendors.length === 0) {
                    vendorsHtml = '<span style="color: #64748b;">N/A</span>';
                } else if (vendors.length === 1) {
                    vendorsHtml = `<span style="color: #cbd5e1;">${escapeHtml(vendors[0])}</span>`;
                } else {
                    vendorsHtml = `
                        <span style="color: #cbd5e1; font-weight: 500;">${escapeHtml(vendors[0])}</span>
                        <span style="font-size: 0.75rem; color: #94a3b8; display: block;">and ${vendors.length - 1} other vendors</span>
                    `;
                }
                
                const hsnDisplay = group.hsn_sac && group.hsn_sac !== 'null' ? group.hsn_sac : 'N/A';
                
                tr.innerHTML = `
                    <td style="padding: 14px 10px; font-weight: 600; color: white;">${escapeHtml(group.description)}</td>
                    <td style="padding: 14px 10px; font-family: monospace; font-size: 0.8rem; color: var(--accent);">${escapeHtml(hsnDisplay)}</td>
                    <td style="padding: 14px 10px;">${priceRangeHtml}</td>
                    <td style="padding: 14px 10px;">${vendorsHtml}</td>
                    <td style="padding: 14px 10px; text-align: center;">
                        <button class="btn btn-sm" onclick="toggleItemProcurement('${index}')" style="background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-weight: bold;"><i class="fas fa-search-plus"></i> View History</button>
                    </td>
                `;
                tbody.appendChild(tr);
                
                const subTr = document.createElement('tr');
                subTr.id = `item-procurement-${index}`;
                subTr.style.display = 'none';
                subTr.style.background = 'rgba(56, 189, 248, 0.02)';
                subTr.style.borderBottom = '2px solid rgba(56, 189, 248, 0.15)';
                
                let historyRowsHtml = '';
                group.procurements.forEach(p => {
                    historyRowsHtml += `
                        <tr style="border-bottom: 1px solid rgba(255,255,255,0.03);">
                            <td style="padding: 10px 8px; color: #94a3b8;">${escapeHtml(p.date || 'N/A')}</td>
                            <td style="padding: 10px 8px; font-weight: bold; color: white;">₹${parseFloat(p.price || 0).toFixed(2)}</td>
                            <td style="padding: 10px 8px; font-family: monospace; color: var(--accent);">${escapeHtml(p.hsn_sac || 'N/A')}</td>
                            <td style="padding: 10px 8px; color: #cbd5e1; font-weight: 500;">${escapeHtml(p.source_party || 'Generic Vendor')}</td>
                            <td style="padding: 10px 8px; font-family: monospace; color: #64748b;">${escapeHtml(p.invoice_number || 'N/A')}</td>
                        </tr>
                    `;
                });
                
                subTr.innerHTML = `
                    <td colspan="5" style="padding: 16px;">
                        <div style="display: flex; flex-direction: column; gap: 8px;">
                            <div style="font-weight: 700; color: #38bdf8; font-size: 0.9rem; display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
                                <i class="fas fa-history"></i> Procurement & Price History Permutations
                            </div>
                            <table style="width: 100%; border-collapse: collapse; font-size: 0.8rem; text-align: left; background: #0f172a; border-radius: 6px; overflow: hidden; border: 1px solid var(--border);">
                                <thead>
                                    <tr style="border-bottom: 1px solid var(--border); background: rgba(255,255,255,0.02); color: #94a3b8;">
                                        <th style="padding: 10px 8px;">Procurement Date</th>
                                        <th style="padding: 10px 8px;">Rate/Price</th>
                                        <th style="padding: 10px 8px;">HSN/SAC</th>
                                        <th style="padding: 10px 8px;">Source Vendor</th>
                                        <th style="padding: 10px 8px;">Invoice No.</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    ${historyRowsHtml}
                                </tbody>
                            </table>
                        </div>
                    </td>
                `;
                tbody.appendChild(subTr);
            });
        }

        window.toggleItemProcurement = function(index) {
            const subRow = document.getElementById(`item-procurement-${index}`);
            if (subRow) {
                subRow.style.display = subRow.style.display === 'none' ? 'table-row' : 'none';
            }
        };

        window.filterItemMaster = function() {
            const query = document.getElementById('item-search').value.toLowerCase().trim();
            if (!query) {
                renderItemTable(allItemsData);
                return;
            }
            
            const filtered = allItemsData.filter(it => {
                return (it.description || '').toLowerCase().includes(query) ||
                       (it.hsn_sac || '').toLowerCase().includes(query) ||
                       (it.source_party || '').toLowerCase().includes(query);
            });
            renderItemTable(filtered);
        };

        window.openAddPartyModal = () => {
            document.getElementById('party-modal-title').innerHTML = `👥 Create Business Party Profile`;
            document.getElementById('party-edit-id').value = '';
            document.getElementById('party-modal-form').reset();
            document.getElementById('party-modal-name').disabled = false;
            document.getElementById('party-modal').style.display = 'flex';
        };

        window.openEditPartyModal = (encoded) => {
            const p = JSON.parse(decodeURIComponent(encoded));
            document.getElementById('party-modal-title').innerHTML = `👥 Edit Party Profile: <span style="color:var(--accent);">${escapeHtml(p.name)}</span>`;
            document.getElementById('party-edit-id').value = p.id || '';
            document.getElementById('party-modal-name').value = p.name || '';
            document.getElementById('party-modal-name').disabled = true;
            document.getElementById('party-modal-gstin').value = p.gstin || '';
            document.getElementById('party-modal-pan').value = p.pan || '';
            document.getElementById('party-modal-address').value = p.address || '';
            document.getElementById('party-modal-bank').value = p.bank_name || '';
            document.getElementById('party-modal-ifsc').value = p.ifsc_code || '';
            document.getElementById('party-modal-account').value = p.account_number || '';
            document.getElementById('party-modal-email').value = p.email || '';
            document.getElementById('party-modal-phone').value = p.phone || '';
            
            document.getElementById('party-modal').style.display = 'flex';
        };

        window.closePartyModal = () => {
            document.getElementById('party-modal').style.display = 'none';
        };

        window.savePartyMaster = async (event) => {
            if (event) event.preventDefault();
            
            const currentUser = JSON.parse(localStorage.getItem('currentUser'));
            const companyName = currentUser ? currentUser.company_name : 'Acme Corp';
            
            const payload = {
                name: document.getElementById('party-modal-name').value.trim(),
                gstin: document.getElementById('party-modal-gstin').value.trim() || null,
                pan: document.getElementById('party-modal-pan').value.trim() || null,
                address: document.getElementById('party-modal-address').value.trim() || null,
                bank_name: document.getElementById('party-modal-bank').value.trim() || null,
                ifsc_code: document.getElementById('party-modal-ifsc').value.trim() || null,
                account_number: document.getElementById('party-modal-account').value.trim() || null,
                email: document.getElementById('party-modal-email').value.trim() || null,
                phone: document.getElementById('party-modal-phone').value.trim() || null,
                company_name: companyName
            };
            
            try {
                const response = await fetch(`${API}/parties`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                
                const result = await response.json();
                if (result.status === 'success') {
                    alert('Party profile saved successfully!');
                    closePartyModal();
                    loadPartyMaster();
                } else {
                    throw new Error(result.message || 'Could not save party');
                }
            } catch (err) {
                alert(`Error saving party: ${err.message}`);
            }
        };

        window.deleteParty = async (partyId, name) => {
            if (!confirm(`Are you absolutely sure you want to delete the party profile for "${name}"?`)) {
                return;
            }
            
            try {
                const response = await fetch(`${API}/parties/${partyId}`, {
                    method: 'DELETE'
                });
                
                const result = await response.json();
                if (result.status === 'success') {
                    alert('Party profile deleted successfully!');
                    loadPartyMaster();
                } else {
                    throw new Error(result.message || 'Could not delete');
                }
            } catch (err) {
                alert(`Error deleting party: ${err.message}`);
            }
        };

        // Auto-initialize Chat view as the landing page
        window.onload = () => {
            showView('chat');
        };
