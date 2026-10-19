use anyhow::{anyhow, Result};
use serde::Serialize;
use transcribe_cpp::{Backend, Device, DeviceType, ModelOptions};

#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ComputeDevice {
    Auto,
    Cpu,
    Igpu,
    Gpu,
}

impl ComputeDevice {
    pub fn parse(value: &str) -> Result<Self> {
        match value {
            "auto" => Ok(Self::Auto),
            "cpu" => Ok(Self::Cpu),
            "igpu" => Ok(Self::Igpu),
            "gpu" => Ok(Self::Gpu),
            _ => Err(anyhow!(
                "invalid compute device '{value}'; use auto, cpu, igpu, or gpu"
            )),
        }
    }

    pub fn select(self, devices: &[Device]) -> Result<(ModelOptions, String)> {
        if self == Self::Auto {
            return Ok((ModelOptions::default(), "Automatic".into()));
        }
        let wanted = match self {
            Self::Cpu => DeviceType::Cpu,
            Self::Igpu => DeviceType::Igpu,
            Self::Gpu => DeviceType::Gpu,
            Self::Auto => unreachable!(),
        };
        let device = devices.iter().find(|device| device.device_type == wanted)
            .ok_or_else(|| anyhow!("requested {self:?} transcription device is unavailable; choose another device in Settings"))?;
        let options = ModelOptions {
            backend: if self == Self::Cpu {
                Backend::Cpu
            } else {
                Backend::Auto
            },
            device: Some(device.clone()),
        };
        Ok((options, device.description.clone()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn automatic_selection_keeps_upstream_defaults() {
        assert_eq!(
            ComputeDevice::Auto.select(&[]).unwrap().0,
            ModelOptions::default()
        );
    }

    #[test]
    fn unavailable_device_fails_without_automatic_fallback() {
        for selection in [ComputeDevice::Cpu, ComputeDevice::Gpu, ComputeDevice::Igpu] {
            assert!(selection.select(&[]).is_err());
        }
        assert!(ComputeDevice::parse("cuda").is_err());
        for name in ["auto", "cpu", "igpu", "gpu"] {
            assert!(ComputeDevice::parse(name).is_ok());
        }
    }
}
