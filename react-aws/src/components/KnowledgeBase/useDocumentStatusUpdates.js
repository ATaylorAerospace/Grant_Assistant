import { useEffect, useState } from 'react';
import { generateClient } from 'aws-amplify/api';
import { fetchAuthSession } from 'aws-amplify/auth';

const client = generateClient();

/**
 * Custom hook for real-time document status updates via GraphQL subscriptions
 *
 * Usage:
 * const { statusUpdates, clearUpdate } = useDocumentStatusUpdates();
 *
 * statusUpdates will contain the latest status change for each document
 */
const useDocumentStatusUpdates = () => {
  const [statusUpdates, setStatusUpdates] = useState({});

  useEffect(() => {
    // Subscribe to document status changes
    // Note: onDocumentStatusChanged returns AWSJSON (scalar), not an object with fields
    const onDocumentStatusChangeSubscription = `
      subscription OnDocumentStatusChanged {
        onDocumentStatusChanged
      }
    `;

    let sub;
    let cancelled = false;
    let hasShownError = false;

    // The subscription is not filtered server-side (the payload is an AWSJSON
    // scalar, which cannot carry a subscription filter), so every subscriber
    // receives every user's events. Resolve the current user's id up front and
    // only surface events for their own documents.
    const start = async () => {
      let currentUserId = null;
      try {
        const session = await fetchAuthSession();
        currentUserId = session.tokens?.idToken?.payload?.sub
          || session.tokens?.accessToken?.payload?.sub
          || null;
      } catch (e) {
        console.warn('⚠️ Could not resolve current user for status updates:', e);
      }
      if (cancelled) return;
      if (!currentUserId) {
        console.info('💡 Real-time updates disabled: no authenticated user id.');
        return;
      }

    try {
      sub = client.graphql({
        query: onDocumentStatusChangeSubscription,
        authMode: 'userPool'
      }).subscribe({
        next: ({ data }) => {
          // Parse the JSON string returned by the subscription
          let update;
          try {
            update = typeof data.onDocumentStatusChanged === 'string'
              ? JSON.parse(data.onDocumentStatusChanged)
              : data.onDocumentStatusChanged;
          } catch (e) {
            console.error('❌ Failed to parse subscription data:', e);
            return;
          }

          // Ignore other users' events (see note above).
          if (!update || update.userId !== currentUserId) {
            return;
          }

          console.log('📡 Document status update received:', update);

          // Store the update keyed by documentId
          setStatusUpdates(prev => ({
            ...prev,
            [update.documentId]: {
              ...update,
              timestamp: new Date().toISOString()
            }
          }));

          // Show toast notification
          showToast(update);
        },
        error: (error) => {
          console.warn('⚠️ Subscription error (real-time updates disabled):', error);

          // Only show error once to avoid spam
          if (!hasShownError) {
            hasShownError = true;
            // Don't show error toast - subscriptions are optional
            // Users can still manually refresh to see updates
            console.info('💡 Real-time updates unavailable. Documents will update when you refresh the list.');
          }
        }
      });
    } catch (error) {
      console.warn('⚠️ Failed to set up subscription (real-time updates disabled):', error);
      // Subscriptions are optional - don't show error to user
    }
    };

    start();

    // Cleanup on unmount (also covers an unmount that happens before start() finishes)
    return () => {
      cancelled = true;
      if (sub) {
        sub.unsubscribe();
      }
    };
  }, []);

  const clearUpdate = (documentId) => {
    setStatusUpdates(prev => {
      const newUpdates = { ...prev };
      delete newUpdates[documentId];
      return newUpdates;
    });
  };

  const clearAllUpdates = () => {
    setStatusUpdates({});
  };

  return {
    statusUpdates,
    clearUpdate,
    clearAllUpdates
  };
};

/**
 * Show a toast notification for status updates
 */
const showToast = (update) => {
  const toastContainer = getOrCreateToastContainer();

  const toast = document.createElement('div');
  toast.style.cssText = `
    padding: 12px 16px;
    margin-bottom: 10px;
    border-radius: 8px;
    box-shadow: 0 4px 6px rgba(0,0,0,0.1);
    display: flex;
    align-items: center;
    gap: 10px;
    animation: slideIn 0.3s ease-out;
    max-width: 400px;
    font-size: 14px;
  `;

  let icon, bgColor, textColor, message;

  switch (update.status) {
    case 'processing':
      icon = '⚙️';
      bgColor = '#fef3c7';
      textColor = '#92400e';
      message = `Processing ${update.filename}...`;
      break;
    case 'ready':
      icon = '✅';
      bgColor = '#d1fae5';
      textColor = '#065f46';
      message = `${update.filename} is ready!`;
      break;
    case 'failed':
      icon = '❌';
      bgColor = '#fee2e2';
      textColor = '#991b1b';
      message = `Failed to process ${update.filename}`;
      break;
    default:
      icon = 'ℹ️';
      bgColor = '#dbeafe';
      textColor = '#1e40af';
      message = `${update.filename} status: ${update.status}`;
  }

  toast.style.backgroundColor = bgColor;
  toast.style.color = textColor;

  // Create elements safely to prevent XSS (don't use innerHTML with user data)
  const iconSpan = document.createElement('span');
  iconSpan.style.fontSize = '20px';
  iconSpan.textContent = icon;

  const messageSpan = document.createElement('span');
  messageSpan.style.flex = '1';
  messageSpan.style.fontWeight = '500';
  messageSpan.textContent = message; // Use textContent instead of innerHTML

  const closeButton = document.createElement('button');
  closeButton.textContent = '×';
  closeButton.style.cssText = `
    background: none;
    border: none;
    color: ${textColor};
    cursor: pointer;
    font-size: 18px;
    padding: 0;
    opacity: 0.7;
  `;
  closeButton.onclick = () => toast.remove();

  toast.appendChild(iconSpan);
  toast.appendChild(messageSpan);
  toast.appendChild(closeButton);

  toastContainer.appendChild(toast);

  // Auto-remove after 5 seconds
  setTimeout(() => {
    if (toast.parentElement) {
      toast.style.animation = 'slideOut 0.3s ease-out';
      setTimeout(() => toast.remove(), 300);
    }
  }, 5000);
};

/**
 * Get or create the toast container
 */
const getOrCreateToastContainer = () => {
  let container = document.getElementById('kb-toast-container');

  if (!container) {
    container = document.createElement('div');
    container.id = 'kb-toast-container';
    container.style.cssText = `
      position: fixed;
      top: 20px;
      right: 20px;
      z-index: 10000;
    `;
    document.body.appendChild(container);

    // Add animations
    const style = document.createElement('style');
    style.textContent = `
      @keyframes slideIn {
        from {
          transform: translateX(400px);
          opacity: 0;
        }
        to {
          transform: translateX(0);
          opacity: 1;
        }
      }
      @keyframes slideOut {
        from {
          transform: translateX(0);
          opacity: 1;
        }
        to {
          transform: translateX(400px);
          opacity: 0;
        }
      }
    `;
    document.head.appendChild(style);
  }

  return container;
};

export default useDocumentStatusUpdates;
