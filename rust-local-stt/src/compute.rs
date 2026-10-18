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
        let options = if self == Self::Cpu {
            ModelOptions {
                backend: Backend::Cpu,
                gpu_device: 0,
            }
        } else {
            let index = device
                .index
                .ok_or_else(|| anyhow!("compute device has no registry index"))?;
            let index = i32::try_from(index)?;
            // The patched native API reserves -1 for explicit registry index zero.
            ModelOptions {
                backend: Backend::Auto,
                gpu_device: if index == 0 { -1 } else { index },
            }
        };
        Ok((options, device.description.clone()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn device(kind: DeviceType, index: usize) -> Device {
        Device {
            name: format!("device{index}"),
            description: format!("Device {index}"),
            kind: "vulkan".into(),
            device_type: kind,
            device_id: None,
            memory_total: 0,
            memory_free: 0,
            index: Some(index),
        }
    }

    #[test]
    fn selection_pins_the_requested_class_including_registry_zero() {
        let devices = [
            device(DeviceType::Igpu, 0),
            device(DeviceType::Gpu, 1),
            device(DeviceType::Cpu, 2),
        ];
        assert_eq!(
            ComputeDevice::Igpu.select(&devices).unwrap().0.gpu_device,
            -1
        );
        assert_eq!(ComputeDevice::Gpu.select(&devices).unwrap().0.gpu_device, 1);
        assert_eq!(
            ComputeDevice::Cpu.select(&devices).unwrap().0.backend,
            Backend::Cpu
        );
        assert_eq!(
            ComputeDevice::Auto.select(&devices).unwrap().0,
            ModelOptions::default()
        );
    }

    #[test]
    fn missing_device_fails_instead_of_falling_back_to_another_class() {
        let devices = [device(DeviceType::Cpu, 0)];
        assert!(ComputeDevice::Gpu.select(&devices).is_err());
        assert!(ComputeDevice::Igpu.select(&devices).is_err());
        assert!(ComputeDevice::parse("cuda").is_err());
        for name in ["auto", "cpu", "igpu", "gpu"] {
            assert!(ComputeDevice::parse(name).is_ok());
        }
    }
}
