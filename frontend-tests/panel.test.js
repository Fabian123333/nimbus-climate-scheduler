import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

function climateState(mode, minTemp = 5, maxTemp = 35) {
  return {
    state: mode,
    attributes: {
      friendly_name: mode,
      current_temperature: 20,
      temperature: 20,
      hvac_modes: ["off", mode],
      min_temp: minTemp,
      max_temp: maxTemp,
      target_temp_step: 0.5,
    },
  };
}

beforeAll(async () => {
  await import(
    "../custom_components/nimbus_climate_scheduler/frontend/nimbus-climate-scheduler-panel.js"
  );
});

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  document.body.replaceChildren();
  localStorage.clear();
  vi.useRealTimers();
});

describe("Nimbus Climate Scheduler panel", () => {
  function editingPanel() {
    const panel = document.createElement("nimbus-climate-scheduler-panel");
    panel.render = vi.fn();
    panel.persistZone = vi.fn();
    panel._hass = { states: { "climate.room": climateState("heat") } };
    const zone = panel.makeZoneFromClimate({ entity: "climate.room" });
    panel.zones = [zone];
    return { panel, zone, schedule: zone.modes.heat };
  }

  it("edits matching days together without changing saved schedules", () => {
    const { panel, zone, schedule } = editingPanel();
    const saved = structuredClone(schedule.savedWeek);
    expect(panel.getDayGroups(zone)).toEqual([["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]]);

    for (let i = 0; i < 7; i++) panel.addSetpoint(zone, "Mon");

    expect(schedule.draftWeek.Mon).toHaveLength(8);
    expect(schedule.draftWeek.Sun).toEqual(schedule.draftWeek.Mon);
    expect(schedule.draftWeek.Sun).not.toBe(schedule.draftWeek.Mon);
    expect(schedule.savedWeek).toEqual(saved);
    expect(schedule.dirty).toBe(true);

    for (let i = 0; i < 7; i++) panel.removeSetpoint(zone, "Mon", 1);
    panel.removeSetpoint(zone, "Mon", 0);
    expect(schedule.draftWeek.Mon).toHaveLength(1);
    expect(schedule.draftWeek.Mon[0].minute).toBe(0);
  });

  it("preserves distinct days and only saves the chosen days", async () => {
    const { panel, zone, schedule } = editingPanel();
    schedule.savedWeek.Sun[0].temp = schedule.draftWeek.Sun[0].temp = 17;
    expect(panel.getDayGroups(zone)).toHaveLength(2);
    panel.daySelection = { zoneId: zone.id, mode: "heat", sourceDay: "Mon", days: ["Mon", "Wed"] };
    panel.updateDraftDay(zone, "Mon", (points) => { points[0].temp = 22; });
    expect(schedule.draftWeek.Wed[0].temp).toBe(22);
    expect(schedule.draftWeek.Tue[0].temp).toBe(20);
    expect(schedule.draftWeek.Sun[0].temp).toBe(17);
    panel.backendSyncComplete = true;
    panel._hass.callWS = vi.fn().mockResolvedValue({ revision: 1 });
    await panel.saveZoneDays(zone, "heat", ["Mon"]);
    expect(schedule.savedWeek.Mon[0].temp).toBe(22);
    expect(schedule.savedWeek.Wed[0].temp).toBe(20);
    expect(schedule.dirty).toBe(true);
    expect(panel._hass.callWS.mock.calls[0][0].data).not.toHaveProperty("activeScheduleMode");
  });

  it("keeps failed saves as editable drafts", async () => {
    const { panel, zone, schedule } = editingPanel();
    panel.backendSyncComplete = true;
    panel._hass.callWS = vi.fn().mockRejectedValue({ code: "conflict", message: "Changed elsewhere" });
    panel.updateDraftDay(zone, "Mon", (points) => { points[0].temp = 22; });

    expect(await panel.saveZoneMode(zone, "heat")).toBe(false);
    expect(schedule.savedWeek.Mon[0].temp).toBe(20);
    expect(schedule.draftWeek.Mon[0].temp).toBe(22);
    expect(schedule.dirty).toBe(true);
    expect(panel.conflictZoneId).toBe(zone.id);
    expect(panel._hass.callWS).toHaveBeenCalledTimes(1);
  });

  it("receives automation pauses without overwriting drafts or writing back", async () => {
    const { panel, zone, schedule } = editingPanel();
    const data = {
      scheduleMode: "heat", activeScheduleMode: null, resumeScheduleMode: "heat", revision: 3,
      modes: structuredClone(zone.modes),
    };
    panel.getOrCreateStoreEntry(zone.id).revision = 3;
    panel.updateDraftDay(zone, "Mon", (points) => { points[0].temp = 22; });
    panel._hass.callWS = vi.fn().mockResolvedValue({
      climates: [{ entity: zone.entityId, name: "Room" }], zones: { [zone.entityId]: data },
    });

    await panel.syncBackendSchedules();

    expect(panel.zones[0].activeScheduleMode).toBeNull();
    expect(panel.zones[0].modes.heat.draftWeek.Mon[0].temp).toBe(22);
    expect(schedule.savedWeek.Mon[0].temp).toBe(20);
    expect(panel._hass.callWS).toHaveBeenCalledExactlyOnceWith({ type: "nimbus_climate_scheduler/get_zones" });
  });

  it("restores existing five-interval schedules and drafts without changing times", () => {
    const points = [0, 120, 240, 555, 810].map((minute) => ({ minute, temp: 19.5 }));
    const week = Object.fromEntries(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((day) => [day, points]));
    const draft = structuredClone(week);
    draft.Tue[1].temp = 21;
    localStorage.setItem("nimbus-climate-scheduler:v1:climate.room", JSON.stringify({
      scheduleMode: "heat", activeScheduleMode: null,
      modes: { heat: { savedWeek: week, draftWeek: draft } },
    }));
    const { zone, schedule } = editingPanel();
    expect(schedule.savedWeek).toEqual(week);
    expect(schedule.draftWeek).toEqual(draft);
    expect(zone.activeScheduleMode).toBeNull();
    expect(schedule.dirty).toBe(true);
  });

  it("restores connection listeners after a disconnect", () => {
    const media = {
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    };
    window.matchMedia = vi.fn(() => media);
    const panel = document.createElement("nimbus-climate-scheduler-panel");

    document.body.append(panel);
    expect(media.addEventListener).toHaveBeenCalledTimes(1);
    panel.remove();
    expect(media.removeEventListener).toHaveBeenCalledTimes(1);
    document.body.append(panel);
    expect(media.addEventListener).toHaveBeenCalledTimes(2);
  });

  it("disables a grouped zone with no common HVAC mode", () => {
    const panel = document.createElement("nimbus-climate-scheduler-panel");
    panel._hass = {
      states: {
        "climate.heat_only": climateState("heat"),
        "climate.cool_only": climateState("cool"),
      },
    };

    const zone = panel.makeZoneFromClimate(
      { entity: "climate.heat_only" },
      {
        name: "Mixed",
        entityIds: ["climate.heat_only", "climate.cool_only"],
      },
    );

    expect(zone.capabilitiesCompatible).toBe(false);
    expect(zone.activeScheduleMode).toBeNull();
    expect(panel.renderZoneSwitch(zone).disabled).toBe(true);
    panel.zones = [zone];
    expect(panel.buildApplyPlans()).toEqual([]);
  });

  it("disables a grouped zone without a common temperature range", () => {
    const panel = document.createElement("nimbus-climate-scheduler-panel");
    panel._hass = {
      states: {
        "climate.low_range": climateState("heat", 5, 15),
        "climate.high_range": climateState("heat", 20, 30),
      },
    };

    const zone = panel.makeZoneFromClimate(
      { entity: "climate.low_range" },
      {
        name: "No overlap",
        entityIds: ["climate.low_range", "climate.high_range"],
      },
    );

    expect(zone.capabilitiesCompatible).toBe(false);
    expect(zone.capabilityIssues.join(" ")).toContain("temperature range");
    expect(panel.renderZoneSwitch(zone).disabled).toBe(true);
  });

  it("does not apply the previous schedule when saving fails", async () => {
    const panel = document.createElement("nimbus-climate-scheduler-panel");
    panel.backendSyncComplete = true;
    panel._hass = { callWS: vi.fn() };
    panel.saveZoneToBackend = vi.fn().mockRejectedValue(new Error("save failed"));
    panel.renderIfReady = vi.fn();
    panel.render = vi.fn();

    await panel.saveAndApply({ name: "Living room" });

    expect(panel._hass.callWS).not.toHaveBeenCalled();
    expect(panel.applyResultData.failed).toHaveLength(1);
  });

  it("leaves manual control alone in the frontend fallback", async () => {
    const panel = document.createElement("nimbus-climate-scheduler-panel");
    panel._hass = { callService: vi.fn() };
    panel.backendSyncComplete = false;
    panel.zones = [{
      id: "climate.manual",
      entityId: "climate.manual",
      entityIds: ["climate.manual"],
      activeScheduleMode: null,
      hvacMode: "heat",
      targetTemp: 20,
      capabilitiesCompatible: true,
    }];

    await panel.runSchedulerTick();

    expect(panel._hass.callService).not.toHaveBeenCalled();
  });
});
