"use client";

import { useState } from "react";

import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingState, Spinner } from "@/components/ui/states";
import { useAsyncResource } from "@/hooks/use-async-resource";
import { changeMemberRole, listMembers, removeMember } from "@/lib/api/endpoints";
import {
  assignableRoles,
  canRemoveMember,
  countOwners,
  isAdministrator,
  type Actor,
} from "@/lib/auth/permissions";
import { useApi, useAuthenticatedSession, useSessionStore } from "@/lib/auth/session-context";
import type { MemberResponse, MemberRole, MembershipStatus } from "@/types/organization";

/**
 * The backend's maximum page size.
 *
 * Asked for in full because the owner count is derived from the result: a
 * partial list could make the UI believe the last owner is replaceable. The
 * backend counts owners itself before allowing anything, so the worst case is
 * an offered button that then fails - see the note in `permissions.ts`.
 */
const PAGE_SIZE = 200;

const STATUS_TONES: Record<MembershipStatus, BadgeTone> = {
  active: "ok",
  invited: "warn",
  suspended: "danger",
};

export function MembersPanel() {
  const { user, active } = useAuthenticatedSession();
  const api = useApi();
  const store = useSessionStore();

  const { state, reload } = useAsyncResource(
    (signal) => listMembers(api, { limit: PAGE_SIZE, signal }),
    // Switching organization makes the loaded list wrong, not stale.
    { key: active?.organization.id ?? "" },
  );

  const [pendingUserId, setPendingUserId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);
  const [confirmingUserId, setConfirmingUserId] = useState<string | null>(null);

  if (active === null) return null;

  const actor: Actor = { userId: user.id, role: active.role };

  async function run(userId: string, action: () => Promise<void>): Promise<void> {
    setActionError(null);
    setPendingUserId(userId);
    try {
      await action();
    } catch (error) {
      // The backend refused. Its reasons are authoritative and its message is
      // written for a client, so it is shown as-is by the shared mapping.
      setActionError(error);
    } finally {
      setPendingUserId(null);
      setConfirmingUserId(null);
    }
  }

  const onChangeRole = (member: MemberResponse, role: MemberRole) =>
    run(member.user.id, async () => {
      await changeMemberRole(api, member.user.id, role);
      reload();
    });

  const onRemove = (member: MemberResponse) =>
    run(member.user.id, async () => {
      await removeMember(api, member.user.id);
      // Removing yourself changes which organizations you are in, so the
      // session has to be re-read rather than assumed.
      if (member.user.id === user.id) await store.refreshUser();
      reload();
    });

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Members</CardTitle>
          <CardDescription>
            Everyone in {active.organization.name}.{" "}
            {isAdministrator(active.role)
              ? "You can change roles and remove members."
              : "Only an owner or admin can make changes."}
          </CardDescription>
        </div>
        {state.kind === "ready" && <Badge>{state.data.length}</Badge>}
      </CardHeader>

      {actionError !== null && (
        <ErrorState className="mt-4" error={actionError} onRetry={() => setActionError(null)} />
      )}

      {state.kind === "loading" && <LoadingState label="Loading members ..." />}

      {state.kind === "error" && (
        <ErrorState className="mt-4" error={state.error} onRetry={reload} />
      )}

      {state.kind === "ready" && state.data.length === 0 && (
        <div className="mt-4">
          <EmptyState title="No members">
            This organization has no members the API will show you.
          </EmptyState>
        </div>
      )}

      {state.kind === "ready" && state.data.length > 0 && (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[34rem] text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-ink-muted uppercase">
                <th scope="col" className="py-2 pr-3 font-medium">
                  Member
                </th>
                <th scope="col" className="py-2 pr-3 font-medium">
                  Role
                </th>
                <th scope="col" className="py-2 pr-3 font-medium">
                  Status
                </th>
                <th scope="col" className="py-2 text-right font-medium">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {state.data.map((member) => (
                <MemberRow
                  key={member.id}
                  member={member}
                  actor={actor}
                  ownerCount={countOwners(state.data)}
                  busy={pendingUserId === member.user.id}
                  disabled={pendingUserId !== null}
                  confirming={confirmingUserId === member.user.id}
                  onConfirm={() => setConfirmingUserId(member.user.id)}
                  onCancelConfirm={() => setConfirmingUserId(null)}
                  onChangeRole={(role) => void onChangeRole(member, role)}
                  onRemove={() => void onRemove(member)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

interface MemberRowProps {
  member: MemberResponse;
  actor: Actor;
  ownerCount: number;
  busy: boolean;
  disabled: boolean;
  confirming: boolean;
  onConfirm: () => void;
  onCancelConfirm: () => void;
  onChangeRole: (role: MemberRole) => void;
  onRemove: () => void;
}

function MemberRow({
  member,
  actor,
  ownerCount,
  busy,
  disabled,
  confirming,
  onConfirm,
  onCancelConfirm,
  onChangeRole,
  onRemove,
}: MemberRowProps) {
  const target = { userId: member.user.id, role: member.role };
  const isSelf = member.user.id === actor.userId;

  // What to offer, mirrored from the backend's rules. Anything not offered
  // would be refused there anyway; this only avoids the pointless click.
  const roles = assignableRoles(actor, target, ownerCount);
  const canEditRole = roles.length > 1;
  const removal = canRemoveMember(actor, target, ownerCount);

  const name = [member.user.first_name, member.user.last_name].filter(Boolean).join(" ").trim();

  return (
    <tr>
      <td className="py-2.5 pr-3">
        <div className="font-medium">
          {name || member.user.email}
          {isSelf && <span className="ml-1.5 text-xs font-normal text-ink-muted">(you)</span>}
        </div>
        {name && <div className="text-xs text-ink-muted">{member.user.email}</div>}
      </td>

      <td className="py-2.5 pr-3">
        {canEditRole ? (
          <select
            aria-label={`Role for ${member.user.email}`}
            value={member.role}
            disabled={disabled}
            onChange={(event) => onChangeRole(event.target.value as MemberRole)}
            className="rounded-lg border border-line bg-surface px-2 py-1 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
          >
            {roles.map((role) => (
              <option key={role} value={role}>
                {role}
              </option>
            ))}
          </select>
        ) : (
          <Badge tone="accent">{member.role}</Badge>
        )}
      </td>

      <td className="py-2.5 pr-3">
        <Badge tone={STATUS_TONES[member.status]}>{member.status}</Badge>
      </td>

      <td className="py-2.5 text-right">
        {busy && <Spinner label="Working" />}

        {!busy && confirming && (
          <span className="inline-flex items-center gap-2">
            <span className="text-xs text-ink-muted">{isSelf ? "Leave?" : "Remove?"}</span>
            <Button variant="secondary" className="px-2 py-1 text-xs" onClick={onRemove}>
              Yes
            </Button>
            <Button variant="ghost" className="px-2 py-1 text-xs" onClick={onCancelConfirm}>
              Cancel
            </Button>
          </span>
        )}

        {!busy && !confirming && (
          <Button
            variant="ghost"
            className="px-2 py-1 text-xs"
            disabled={disabled || !removal.allowed}
            title={removal.allowed ? undefined : removal.reason}
            onClick={onConfirm}
          >
            {isSelf ? "Leave" : "Remove"}
          </Button>
        )}
      </td>
    </tr>
  );
}
